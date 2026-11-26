// A lane's alternate source (its MIDI sound) on the chunked engine, and the
// clock maths both engines share (static/js/altSource.js).
//
// The chunked engine plays each stem as a chain of five second chunk nodes;
// the alternate source is one node over the whole in-memory buffer. They stay
// in the same sample frame only if, for every chunk node S and the alternate
// node A, song time at context time c is the same line:
//   S.songStart + S.offset + (c - S.when) * S.rate == A.offset + (c - A.when) * A.rate
// This drives the real engine against a fake clock and checks that line after
// a start, through chunk boundaries, after a seek, a loop wrap, a speed change
// and a join while playing, with the tempo stage and in the tape-effect
// fallback. It also checks the Stem / MIDI / Both gains and the clean-up.
//
// Each sample of the synthetic WAV holds its chunk's index, so a chunk node
// says which song second it starts at. The e2e fixture is 6 seconds long,
// which cannot show a chunk boundary, a loop wrap or a minute of drift.
//
// Run:  node tests/js/alt-source.test.mjs

let passed = 0;
let failed = 0;

function check(name, condition, detail = '') {
  if (condition) {
    passed++;
    console.log(`PASS  ${name}`);
  } else {
    failed++;
    console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ''}`);
  }
}

const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;

// ─── The shared maths ───

const { altSourceStart, laneSourceMode, sourceOn, ALT_JOIN_MARGIN } = await import('../../static/js/altSource.js');

check('a start begins with the stems', JSON.stringify(altSourceStart({
  startCtxTime: 3, startOffset: 42.5, ctxNow: 2.9, bufferDuration: 100,
})) === JSON.stringify({ when: 3, offset: 42.5 }));

{
  // Joining 10 s after a start at song 20: the stems reach 30.05 at now + margin.
  const at = altSourceStart({ startCtxTime: 5, startOffset: 20, ctxNow: 15, bufferDuration: 100, join: true });
  check('a join starts just ahead of now', near(at.when, 15 + ALT_JOIN_MARGIN));
  check('a join starts where the stems will be', near(at.offset, 20 + 10 + ALT_JOIN_MARGIN));
}
{
  // Tape-effect fallback at 0.75x: the sources read 0.75 s of song per second.
  const at = altSourceStart({ startCtxTime: 0, startOffset: 10, srcRate: 0.75, ctxNow: 8, bufferDuration: 100, join: true });
  check('a join at 0.75x follows the source rate', near(at.offset, 10 + (8 + ALT_JOIN_MARGIN) * 0.75));
}
{
  // A count-in: the stems start 2 s from now, so the join waits for them.
  const at = altSourceStart({ startCtxTime: 12, startOffset: 4, ctxNow: 10, bufferDuration: 100, join: true });
  check('a join during a count-in starts with the stems', near(at.when, 12) && near(at.offset, 4));
}
check('past the end of the buffer: no start',
  altSourceStart({ startCtxTime: 0, startOffset: 59.99, ctxNow: 1, bufferDuration: 60, join: true }) === null);
check('at the end of the buffer: no start',
  altSourceStart({ startCtxTime: 0, startOffset: 60, bufferDuration: 60 }) === null);
check('a loop wrap starts again at the loop start', JSON.stringify(altSourceStart({
  startCtxTime: 30.01, startOffset: 10, ctxNow: 30, bufferDuration: 60,
})) === JSON.stringify({ when: 30.01, offset: 10 }));
check('modes', ['stem', 'alt', 'both', 'midi', undefined].map(laneSourceMode).join() === 'stem,alt,both,stem,stem');
check('Stem sounds the stem only', sourceOn('stem', false) === 1 && sourceOn('stem', true) === 0);
check('MIDI sounds the MIDI only', sourceOn('alt', false) === 0 && sourceOn('alt', true) === 1);
check('Both sounds both', sourceOn('both', false) === 1 && sourceOn('both', true) === 1);
check('an unknown mode is Stem', sourceOn('loud', false) === 1 && sourceOn('loud', true) === 0);

// ─── The chunked engine against a fake clock ───

const RATE = 8000;
const CHUNK_SEC = 5;
const TRACK_SEC = 60;
const STEP_SEC = 0.1;

class FakeParam {
  constructor(value = 0) { this.value = value; }
  setTargetAtTime(value) { this.value = value; }
  setValueAtTime(value) { this.value = value; }
  linearRampToValueAtTime(value) { this.value = value; }
  cancelScheduledValues() {}
}

class FakeNode {
  constructor(ctx, type) {
    this.ctx = ctx;
    this.type = type;
    this.connected = new Set();
  }
  connect(destination) { this.connected.add(destination); return destination; }
  disconnect(destination) {
    if (destination === undefined) this.connected.clear();
    else this.connected.delete(destination);
  }
}

class FakeGain extends FakeNode {
  constructor(ctx) {
    super(ctx, 'gain');
    this.gain = new FakeParam(1);
  }
}

class FakeAnalyser extends FakeNode {
  constructor(ctx) { super(ctx, 'analyser'); this.fftSize = 1024; }
}

class FakeSource extends FakeNode {
  constructor(ctx) {
    super(ctx, 'source');
    this.playbackRate = new FakeParam(1);
    this.buffer = null;
    this.when = null;
    this.offset = null;
    this.stopped = false;
    ctx.sources.push(this);
  }
  start(when, offset = 0) { this.when = when; this.offset = offset; }
  stop() { this.stopped = true; }
}

class FakeContext {
  constructor({ worklet }) {
    this.sources = [];
    this.currentTime = 0;
    this.sampleRate = 44100;
    this.state = 'running';
    this.destination = new FakeNode(this, 'destination');
    this.audioWorklet = worklet ? { addModule: async () => {} } : null;
  }
  createGain() { return new FakeGain(this); }
  createAnalyser() { return new FakeAnalyser(this); }
  createBufferSource() { return new FakeSource(this); }
  createBuffer(channels, length, sampleRate) {
    const data = Array.from({ length: channels }, () => new Float32Array(length));
    return { numberOfChannels: channels, length, sampleRate, duration: length / sampleRate, getChannelData: (c) => data[c] };
  }
  async resume() {}
  async close() {}
  addEventListener() {}
  removeEventListener() {}
}

class FakeWorkletNode extends FakeNode {
  constructor(ctx) {
    super(ctx, 'worklet');
    this.parameters = new Map([['tempo', new FakeParam(1)]]);
    this.port = { postMessage() {} };
  }
}

let pendingTick = null;
globalThis.window = { AudioContext: FakeContext };
globalThis.AudioWorkletNode = FakeWorkletNode;
globalThis.requestAnimationFrame = (cb) => { pendingTick = cb; return 1; };
globalThis.cancelAnimationFrame = () => { pendingTick = null; };

// Mono 16-bit, every sample of chunk k holding the value k.
function wavBytes(seconds) {
  const frames = seconds * RATE;
  const bytes = new ArrayBuffer(44 + frames * 2);
  const view = new DataView(bytes);
  const text = (offset, value) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, 'RIFF');
  view.setUint32(4, bytes.byteLength - 8, true);
  text(8, 'WAVE');
  text(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, RATE, true);
  view.setUint32(28, RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, 'data');
  view.setUint32(40, frames * 2, true);
  for (let i = 0; i < frames; i++) view.setInt16(44 + i * 2, Math.floor(i / (CHUNK_SEC * RATE)), true);
  return new Uint8Array(bytes);
}

const WAV = wavBytes(TRACK_SEC);
globalThis.fetch = async (_url, options = {}) => {
  const match = /bytes=(\d+)-(\d+)/.exec(options.headers?.Range || '');
  const start = match ? Number(match[1]) : 0;
  const end = match ? Math.min(Number(match[2]), WAV.length - 1) : WAV.length - 1;
  const slice = WAV.slice(start, end + 1);
  return {
    ok: true,
    status: match ? 206 : 200,
    headers: { get: (name) => (name === 'Content-Range' ? `bytes ${start}-${end}/${WAV.length}` : null) },
    arrayBuffer: async () => slice.buffer,
  };
};

const { createChunkedAudioEngine } = await import('../../static/js/chunkedAudioEngine.js');

const settle = async () => {
  for (let i = 0; i < 6; i++) await new Promise((resolve) => setImmediate(resolve));
};

async function run(ctx, seconds) {
  const until = ctx.currentTime + seconds;
  while (ctx.currentTime < until - 1e-9) {
    ctx.currentTime += STEP_SEC;
    const tick = pendingTick;
    pendingTick = null;
    tick?.();
    await settle();
  }
}

const chunkStart = (node) => Math.round(node.buffer.getChannelData(0)[0] * 32768) * CHUNK_SEC;
// The song-time line a node plays on: song = intercept + c * rate.
const interceptOf = (node, songStart) => songStart + node.offset - node.when * node.playbackRate.value;

/**
 * The live alternate node (started, not stopped) and whether every live chunk
 * node of the bass lane started on its line.
 */
function syncState(ctx, alt) {
  const altNodes = ctx.sources.filter((n) => n.buffer === alt && !n.stopped);
  const chunks = ctx.sources.filter((n) => n.buffer !== alt && !n.stopped && n.when !== null);
  if (altNodes.length !== 1) return { ok: false, detail: `${altNodes.length} live MIDI nodes` };
  const a = altNodes[0];
  const line = interceptOf(a, 0);
  const off = chunks
    .filter((s) => s.when + (s.buffer.duration - s.offset) / s.playbackRate.value > a.when)
    .map((s) => ({ s, d: interceptOf(s, chunkStart(s)) - line, rate: s.playbackRate.value }))
    .filter(({ d, rate }) => !near(d, 0, 1e-6) || rate !== a.playbackRate.value);
  return {
    ok: off.length === 0 && chunks.length > 0,
    node: a,
    detail: off.length ? `${off.length} chunk node(s) off the MIDI line by ${off[0].d}` : `${chunks.length} chunk nodes`,
  };
}

async function verify(label, { worklet, rate = 1 }) {
  const ctx = new FakeContext({ worklet });
  const engine = createChunkedAudioEngine(
    [{ name: 'bass', url: '/bass.wav' }, { name: 'drums', url: '/drums.wav' }],
    { context: ctx },
  );
  check(`${label}: ready`, await engine.ready);
  check(`${label}: says it takes an alternate source`, engine.supportsAltSource === true);
  if (rate !== 1) engine.setPlaybackRate(rate);
  const srcRate = worklet ? 1 : rate;

  const alt = ctx.createBuffer(1, TRACK_SEC * ctx.sampleRate, ctx.sampleRate);
  check(`${label}: a lane without a stem takes none`, engine.addAltSource('piano', alt) === false);
  check(`${label}: the bass lane takes one`, engine.addAltSource('bass', alt) === true);
  check(`${label}: its VU tap is the lane's second`, engine.getAnalysers('bass').length === 2);
  check(`${label}: the visuals get no buffer from it`, engine.getBuffers().size === 0);
  check(`${label}: the song length is the stems'`, engine.getDuration() === TRACK_SEC);

  // Start.
  await engine.play();
  await settle();
  let s = syncState(ctx, alt);
  check(`${label}: on Play the MIDI starts in the chunk's frame, at 0`, s.ok && near(s.node.offset, 0), s.detail);
  check(`${label}: it reads at the sources' rate`, s.node?.playbackRate.value === srcRate);

  // Through two chunk boundaries.
  await run(ctx, 12);
  s = syncState(ctx, alt);
  check(`${label}: 12 s later every chunk is still on the MIDI's line`, s.ok, s.detail);

  // A seek while playing.
  engine.seek(23.7);
  await settle();
  s = syncState(ctx, alt);
  check(`${label}: after a seek the MIDI starts again at the new place`, s.ok && near(s.node.offset, 23.7), s.detail);
  check(`${label}: the MIDI node of before the seek is stopped`, ctx.sources.filter((n) => n.buffer === alt && n.stopped).length >= 1);

  // A loop wrap.
  engine.setLoop(true, 30, 34);
  engine.seek(31);
  await settle();
  await run(ctx, 4.5 / srcRate);
  s = syncState(ctx, alt);
  check(`${label}: after the loop wraps the MIDI starts at the loop start`, s.ok && s.node.offset >= 30 && s.node.offset < 31, `${s.detail}, at ${s.node?.offset}`);
  engine.setLoop(false, 0, 0);

  // A speed change while playing.
  engine.setPlaybackRate(rate === 1 ? 0.8 : 1.25);
  await settle();
  const newSrcRate = worklet ? 1 : (rate === 1 ? 0.8 : 1.25);
  s = syncState(ctx, alt);
  check(`${label}: after a speed change the MIDI is on the chunks' line`, s.ok, s.detail);
  check(`${label}: and reads at the new source rate`, s.node?.playbackRate.value === newSrcRate);
  await run(ctx, 6);
  s = syncState(ctx, alt);
  check(`${label}: 6 s after the speed change it still is`, s.ok, s.detail);

  // A join while playing: the MIDI added mid-song.
  engine.removeAltSource('bass');
  check(`${label}: removed, its node is stopped`, ctx.sources.filter((n) => n.buffer === alt && !n.stopped).length === 0);
  check(`${label}: removed, its VU tap goes`, engine.getAnalysers('bass').length === 1);
  await run(ctx, 1.3);
  engine.addAltSource('bass', alt);
  s = syncState(ctx, alt);
  check(`${label}: joined mid-song it lands on the chunks' line`, s.ok, s.detail);
  check(`${label}: joined just ahead of now`, near(s.node?.when, ctx.currentTime + ALT_JOIN_MARGIN));

  // Pause, then play from there.
  engine.pause();
  check(`${label}: pause stops the MIDI`, ctx.sources.filter((n) => n.buffer === alt && !n.stopped).length === 0);
  const at = engine.getCurrentTime();
  await engine.play();
  await settle();
  s = syncState(ctx, alt);
  check(`${label}: play after pause starts it where it stopped`, s.ok && near(s.node.offset, at), s.detail);

  engine.destroy();
  check(`${label}: destroy stops every MIDI node`, ctx.sources.filter((n) => n.buffer === alt && !n.stopped).length === 0);
  check(`${label}: destroyed, the engine has no lane taps`, engine.getAnalysers('bass').length === 0);
}

await verify('tempo stage, 1x', { worklet: true });
await verify('tempo stage, 0.75x', { worklet: true, rate: 0.75 });
await verify('tape effect, 0.75x', { worklet: false, rate: 0.75 });

// ─── Stem / MIDI / Both, volume and mute ───

{
  const ctx = new FakeContext({ worklet: true });
  // Capture each gain the engine makes, in order: the stems' first, then the MIDI's.
  const made = [];
  const createGain = ctx.createGain.bind(ctx);
  ctx.createGain = () => { const g = createGain(); made.push(g); return g; };
  const engine = createChunkedAudioEngine([{ name: 'bass', url: '/bass.wav' }], { context: ctx });
  await engine.ready;
  const before = made.length;
  const alt = ctx.createBuffer(1, TRACK_SEC * ctx.sampleRate, ctx.sampleRate);
  engine.addAltSource('bass', alt);
  // addAltSource makes the lane gain first, then the EQ slot's three.
  const midiGain = made[before];
  const midiNode = () => ctx.sources.find((n) => n.buffer === alt && !n.stopped);
  await engine.play();
  await settle();
  // The stem's lane gain is what its chunk nodes connect to.
  const stemNode = ctx.sources.find((n) => n.buffer !== alt && !n.stopped);
  const stemGain = [...stemNode.connected][0];
  check('the MIDI node feeds the MIDI lane gain', midiNode().connected.has(midiGain));
  const g = () => [stemGain.gain.value, midiGain.gain.value].join();
  check('Stem (the default): stem on, MIDI off', g() === '1,0', g());
  engine.setLaneSource('bass', 'alt');
  check('MIDI: stem off, MIDI on', g() === '0,1', g());
  engine.setGain('bass', 0.5);
  check('MIDI at half volume: the MIDI at half, the stem still off', g() === '0,0.5', g());
  engine.setLaneSource('bass', 'both');
  check('Both: both at the lane volume', g() === '0.5,0.5', g());
  engine.setGain('bass', 0);
  check('muted (volume 0): both silent', g() === '0,0', g());
  engine.setGain('bass', 1);
  engine.setLaneSource('bass', 'stem');
  check('Stem again: stem on, MIDI off', g() === '1,0', g());
  // A MIDI added while the lane is on MIDI starts at the lane level.
  engine.setLaneSource('bass', 'alt');
  engine.addAltSource('bass', alt);
  const again = made[made.length - 4];
  check('a MIDI added while the lane is on MIDI sounds at once', again.gain.value === 1, String(again.gain.value));
  check('one live MIDI node after re-adding', ctx.sources.filter((n) => n.buffer === alt && !n.stopped).length === 1);
  check('a transpose moves the MIDI with its lane', engine.setStemPitch('bass', 3) === true && engine.getStemPitch('bass') === 3);
  engine.destroy();
}

// ─── The full-decode engine joins on the same line ───

{
  const ctx = new FakeContext({ worklet: true });
  ctx.decodeAudioData = async () => ctx.createBuffer(1, TRACK_SEC * ctx.sampleRate, ctx.sampleRate);
  const { createAudioEngine } = await import('../../static/js/audioEngine.js');
  const engine = createAudioEngine([{ name: 'bass', url: '/bass.wav' }], { context: ctx });
  await engine.ready;
  const alt = ctx.createBuffer(1, TRACK_SEC * ctx.sampleRate, ctx.sampleRate);
  engine.seek(7);
  await engine.play();
  ctx.currentTime += 2.4;
  engine.addAltSource('bass', alt);
  const stemNode = ctx.sources.find((n) => n.buffer !== alt && !n.stopped);
  const midiNode = ctx.sources.find((n) => n.buffer === alt && !n.stopped);
  check('full decode: a join starts just ahead of now', near(midiNode.when, ctx.currentTime + ALT_JOIN_MARGIN));
  check('full decode: a join lands on the stem\'s line', near(interceptOf(midiNode, 0), interceptOf(stemNode, 0)),
    `${interceptOf(midiNode, 0)} vs ${interceptOf(stemNode, 0)}`);
  engine.destroy();
}

console.log(`\n${passed}/${passed + failed} checks passed`);
process.exit(failed === 0 ? 0 : 1);
