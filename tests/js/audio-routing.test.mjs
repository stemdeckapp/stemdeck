import { addVisualOnlyStems, buildPlaybackStems } from '../../static/js/playbackStems.js';

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

const BASE = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other'];
const stem = (name) => ({ name, url: `/${name}.wav` });

{
  const raw = ['original', ...BASE].map(stem);
  const visible = [stem('original'), stem('vocals'), stem('guitar')];
  const playback = buildPlaybackStems(raw, visible, BASE);
  check(
    'original is reconstructed from each unselected raw stem',
    playback.map((item) => `${item.name}:${item.controlName}`).join(',')
      === 'vocals:vocals,guitar:guitar,drums:original,bass:original,piano:original,other:original',
  );
  check(
    'the reconstructed drum source is always unpitched',
    playback.find((item) => item.name === 'drums')?.pitched === false,
  );
  check(
    'melodic sources in the original control group remain pitchable',
    playback.filter((item) => item.controlName === 'original' && item.name !== 'drums')
      .every((item) => item.pitched === true),
  );

  const full = addVisualOnlyStems(playback, visible);
  check(
    'full decode retains original.wav only for waveform rendering',
    full.some((item) => item.name === 'original' && item.visualOnly === true),
  );
}

{
  const raw = [stem('original'), stem('vocals'), stem('bass')];
  const visible = [stem('original'), stem('vocals')];
  const playback = buildPlaybackStems(raw, visible, BASE);
  const fallback = playback.find((item) => item.name === 'original');
  check('an incomplete legacy complement is played exactly once', playback.length === 2);
  check('an incomplete legacy complement stays wholly unpitched', fallback?.pitched === false);
}

{
  const raw = ['original', ...BASE, 'lead_vocals', 'backing_vocals'].map(stem);
  const visible = [stem('original'), stem('lead_vocals'), stem('backing_vocals')];
  const playback = buildPlaybackStems(raw, visible, BASE);
  check(
    'split vocals replace base vocals without doubling them into original',
    !playback.some((item) => item.name === 'vocals'),
  );
}

class FakeParam {
  constructor(value = 0) { this.value = value; }
}

class FakeNode {
  constructor(ctx, type) {
    this.ctx = ctx;
    this.type = type;
    this.connections = [];
    ctx.nodes.push(this);
  }

  connect(destination, output = 0, input = 0) {
    this.connections.push({ destination, output, input });
    return destination;
  }

  disconnect() { this.connections = []; }
}

class FakeGain extends FakeNode {
  constructor(ctx) {
    super(ctx, 'gain');
    this.gain = {
      value: 1,
      setTargetAtTime: (value) => { this.gain.value = value; },
    };
  }
}

class FakeAnalyser extends FakeNode {
  constructor(ctx) {
    super(ctx, 'analyser');
    this.fftSize = 1024;
  }

  getByteTimeDomainData(data) { data.fill(128); }

  getFloatTimeDomainData(data) { data.fill(0); }
}

class FakeSource extends FakeNode {
  constructor(ctx) {
    super(ctx, 'source');
    this.playbackRate = new FakeParam(1);
    this.buffer = null;
    this.stopped = false;
  }

  start() {}
  stop() { this.stopped = true; }
}

class FakeContext {
  constructor() {
    this.nodes = [];
    this.currentTime = 0;
    this.sampleRate = 44100;
    this.state = 'running';
    this.destination = new FakeNode(this, 'destination');
    this.audioWorklet = { addModule: async () => {} };
  }

  createGain() { return new FakeGain(this); }
  createAnalyser() { return new FakeAnalyser(this); }
  createBufferSource() { return new FakeSource(this); }
  createBuffer(channels, length, sampleRate) {
    const data = Array.from({ length: channels }, () => new Float32Array(length));
    return {
      duration: length / sampleRate,
      getChannelData: (channel) => data[channel],
    };
  }
  async decodeAudioData() {
    return { duration: 1, numberOfChannels: 2, sampleRate: 44100, length: 44100 };
  }
  async resume() {}
  async close() {}
}

class FakeWorkletNode extends FakeNode {
  constructor(ctx, name, options) {
    super(ctx, 'worklet');
    this.name = name;
    this.options = options;
    this.parameters = new Map([['tempo', new FakeParam(1)]]);
    this.messages = [];
    this.port = { postMessage: (message) => this.messages.push(message) };
    // The real processor reports its buffering the moment it is constructed,
    // and the engine needs that number to hold the playhead while the pitch
    // buses prime. tests/js/pitch-shift.test.mjs checks the value itself; what
    // matters here is that the engine listens and uses it.
    queueMicrotask(() => this.port.onmessage?.({ data: { type: 'latency', frames: WORKLET_LATENCY_FRAMES } }));
  }
}

// Roughly what the processor reports at 44.1 kHz: its common priming plus the
// unpitched bus's alignment pad. Only the order of magnitude matters here.
const WORKLET_LATENCY_FRAMES = 6541;

globalThis.window = { AudioContext: FakeContext };
globalThis.AudioWorkletNode = FakeWorkletNode;
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};

function wavBytes() {
  const samples = 64;
  const bytes = new ArrayBuffer(44 + samples * 2);
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
  view.setUint32(24, 44100, true);
  view.setUint32(28, 88200, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, 'data');
  view.setUint32(40, samples * 2, true);
  return new Uint8Array(bytes);
}

const WAV = wavBytes();
globalThis.fetch = async (_url, options = {}) => {
  const range = options.headers?.Range;
  let start = 0;
  let end = WAV.length - 1;
  if (range) {
    const match = /bytes=(\d+)-(\d+)/.exec(range);
    if (match) {
      start = Number(match[1]);
      end = Math.min(Number(match[2]), end);
    }
  }
  const slice = start <= end ? WAV.slice(start, end + 1) : new Uint8Array();
  return {
    ok: true,
    status: range ? 206 : 200,
    headers: { get: (name) => name === 'Content-Range' ? `bytes ${start}-${end}/${WAV.length}` : null },
    arrayBuffer: async () => slice.buffer,
  };
};

const { INPUT_COUNT, ZERO_INPUT } = await import('../../static/js/pitchBus.js');
const { createAudioEngine } = await import('../../static/js/audioEngine.js');
const { createChunkedAudioEngine } = await import('../../static/js/chunkedAudioEngine.js');

async function verifyEngine(name, create) {
  const ctx = new FakeContext();
  const sources = [
    { ...stem('vocals'), controlName: 'vocals', pitched: true },
    { ...stem('drums'), controlName: 'original', pitched: true },
    { ...stem('bass'), controlName: 'original', pitched: true },
  ];
  const engine = create(sources, { context: ctx });
  check(`${name} becomes ready`, await engine.ready);

  const worklet = ctx.nodes.find((node) => node.type === 'worklet');
  check(
    `${name} creates one worklet input per semitone`,
    worklet?.options?.numberOfInputs === INPUT_COUNT,
    `got ${worklet?.options?.numberOfInputs}`,
  );

  // A lane's transpose is which input it is wired to, so "what key is this
  // lane in" is answered by walking the graph, not by reading a parameter.
  const bus = (index) => ctx.nodes.find((node) =>
    node.connections.some((edge) => edge.destination === worklet && edge.input === index));
  const busOf = (node) => {
    for (let k = 0; k < INPUT_COUNT; k++) {
      if (node?.connections.some((edge) => edge.destination === bus(k))) return k - ZERO_INPUT;
    }
    return null;
  };
  check(`${name} exposes the unpitched bus for the metronome`, engine.getMasterNode() === bus(ZERO_INPUT));

  const originalAnalysers = engine.getAnalysers('original');
  check(`${name} groups all complement analysers under original`, originalAnalysers.length === 2);
  const drumAnalyser = originalAnalysers[0];
  const melodicAnalyser = originalAnalysers[1];
  check(
    `${name} starts every lane at its own key`,
    busOf(drumAnalyser) === 0 && busOf(melodicAnalyser) === 0,
  );

  engine.setGain('original', 0.37);
  const groupedGains = originalAnalysers.map((analyser) =>
    ctx.nodes.find((node) => node.type === 'gain'
      && node.connections.some((edge) => edge.destination === analyser))?.gain.value);
  check(`${name} applies one mixer control to every complement source`, groupedGains.every((v) => v === 0.37));
  check(`${name} reports pitch support only after worklet setup`, engine.supportsPitchShift() === true);

  // A lane's key is absolute: it is wired to the input for the number it was
  // given, with nothing else added in.
  check(`${name} accepts a lane transpose`, engine.setStemPitch('vocals', -2) === true);
  const vocalBus = busOf(engine.getAnalysers('vocals')[0]);
  check(`${name} puts a lane in the key it was given`, vocalBus === -2, `landed on ${vocalBus}`);
  check(`${name} reports the key it was given`, engine.getStemPitch('vocals') === -2);
  check(`${name} leaves other lanes where they were`, busOf(melodicAnalyser) === 0);

  // A key past the range the DSP is measured over stops at the edge rather
  // than landing somewhere unusable.
  engine.setStemPitch('vocals', 99);
  check(
    `${name} clamps a lane to the offered range`,
    busOf(engine.getAnalysers('vocals')[0]) === 6,
  );

  // One control drives both sources in the `original` group, and only one of
  // them is allowed to move. This is the whole reason the unpitched input
  // exists, and the caller above marked this drum source `pitched: true`.
  engine.setStemPitch('original', 3);
  check(
    `${name} transposes melodic complement sources`,
    busOf(melodicAnalyser) === 3,
    `landed on ${busOf(melodicAnalyser)}`,
  );
  check(
    `${name} never moves drums, whatever it is told`,
    busOf(drumAnalyser) === 0,
    `landed on ${busOf(drumAnalyser)}`,
  );
  check(`${name} reports drums as not pitchable`, engine.isStemPitchable('drums') === false);

  engine.setStemPitch('vocals', 3);

  if (name === 'full-decode engine') {
    engine.play();
    const anchoredAt = engine.getCurrentTime();
    ctx.currentTime += 0.05;
    check(
      'the full-decode playhead waits while a changed pitch stage primes',
      engine.getCurrentTime() === anchoredAt,
    );
    ctx.currentTime += 0.20;
    check('the full-decode playhead advances after DSP priming', engine.getCurrentTime() > anchoredAt);
    engine.pause();
    check(
      'pausing flushes buffered worklet output',
      worklet.messages.at(-1)?.type === 'reset',
    );
  }
  engine.destroy();
}

await verifyEngine('full-decode engine', createAudioEngine);
await verifyEngine('chunked engine', createChunkedAudioEngine);

{
  const ctx = new FakeContext();
  ctx.audioWorklet = null;
  const engine = createAudioEngine([stem('vocals')], { context: ctx });
  check('full-decode fallback audio still becomes ready', await engine.ready);
  check('fallback audio reports that pitch shifting is unavailable', engine.supportsPitchShift() === false);
  // A stepper that moves its own label while the sound never changes is worse
  // than one that plainly cannot: the engine refuses rather than pretending.
  check(
    'fallback audio rejects a misleading pitch change',
    engine.setStemPitch('vocals', 4) === false && engine.getStemPitch('vocals') === 0,
  );
  engine.destroy();
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
