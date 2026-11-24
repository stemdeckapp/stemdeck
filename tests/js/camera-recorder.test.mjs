// The picture's recorder when the camera's sound fails mid-take
// (static/js/video/cameraRecorder.js): the picture recorded so far is kept
// and uploaded, Stop never hangs, and the video block says the camera sound
// failed so the server's sync reason is "camera-mic-failed". A fake
// MediaRecorder stands in for the browser's.
//
// Run:  node tests/js/camera-recorder.test.mjs

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

class FakeTarget {
  constructor() { this.handlers = {}; }
  addEventListener(type, fn, opts) { (this.handlers[type] ||= []).push({ fn, once: opts?.once }); }
  emit(type, ev = {}) {
    const list = this.handlers[type] || [];
    this.handlers[type] = list.filter((h) => !h.once);
    for (const h of list) h.fn(ev);
  }
}

const made = [];
class FakeRecorder extends FakeTarget {
  static isTypeSupported(t) { return t === 'video/webm;codecs=vp8,opus' || t === 'video/webm;codecs=vp8'; }
  constructor(stream, opts = {}) {
    super();
    this.stream = stream;
    this.mimeType = opts.mimeType || 'video/webm';
    this.state = 'inactive';
    this.fireStopOnStop = true;
    made.push(this);
  }
  start() { this.state = 'recording'; this.emit('start'); }
  stop() {
    this.state = 'inactive';
    if (this.fireStopOnStop) this.emit('stop');
  }
  data(bytes) { this.emit('dataavailable', { data: new Blob([new Uint8Array(bytes)], { type: this.mimeType }) }); }
  fail(error) {
    // As Chromium does: error, then the recorder is inactive; "stop" may or may not follow.
    this.state = 'inactive';
    this.emit('error', { error });
  }
}

class FakeStream {
  constructor(tracks) { this.tracks = tracks; }
  getVideoTracks() { return this.tracks.filter((t) => t.kind === 'video'); }
  getAudioTracks() { return this.tracks.filter((t) => t.kind === 'audio'); }
}

globalThis.window = { MediaRecorder: FakeRecorder };
globalThis.MediaRecorder = FakeRecorder;
globalThis.MediaStream = FakeStream;

const { startCameraRecording } = await import('../../static/js/video/cameraRecorder.js');

function videoTrack() {
  return { kind: 'video', getSettings: () => ({ width: 1280, height: 720, frameRate: 30 }) };
}
function audioTrack() {
  const t = new FakeTarget();
  t.kind = 'audio';
  t.readyState = 'live';
  return t;
}

function start(audio, cameraAudio) {
  const uploads = [];
  const rec = startCameraRecording({
    stream: new FakeStream([videoTrack()]),
    takeId: 't0000aaaa',
    perfMsAtFrame0: 100,
    ctxTimeAtFrame0: 1,
    bitsPerSecond: 5e6,
    deviceLabel: 'Cam',
    mirrorPreview: false,
    previewVideo: null,
    audioTrack: audio,
    cameraAudio,
    upload: async (seq, blob) => { uploads.push({ seq, size: blob.size }); },
  });
  return { rec, uploads, recorder: made[made.length - 1] };
}

const within = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('hung')), ms))]);

{
  const audio = audioTrack();
  const { rec, uploads, recorder } = start(audio, { state: 'recorded', label: 'Cam mic' });
  check('the sound is in the recorder', recorder.stream.getAudioTracks().length === 1);
  recorder.data([1, 2, 3, 4]);
  recorder.fail(new Error('audio encoder failed'));
  let block = null;
  try {
    block = await within(rec.stop(), 500);
  } catch (err) {
    check('Stop resolves after a recorder error with no "stop" event', false, String(err));
  }
  check('Stop resolves after a recorder error with no "stop" event', block !== null);
  check('the picture recorded before the error is kept and uploaded', uploads.length === 1 && uploads[0].size === 4);
  check('the block says the camera sound failed', block?.cameraAudio?.state === 'failed', JSON.stringify(block?.cameraAudio));
  check('its label is kept', block?.cameraAudio?.label === 'Cam mic');
  check('the parts count matches what was sent', block?.parts === 1);
}
{
  const audio = audioTrack();
  const { rec, recorder } = start(audio, { state: 'recorded', label: 'Cam mic' });
  recorder.data([9, 9]);
  audio.readyState = 'ended';
  audio.emit('ended');
  check('an unplugged microphone leaves the picture recording', recorder.state === 'recording');
  const block = await within(rec.stop(), 500);
  check('and the block says the camera sound failed', block.cameraAudio?.state === 'failed');
}
{
  const audio = audioTrack();
  const { rec, recorder } = start(audio, { state: 'recorded', label: 'Cam mic' });
  recorder.data([1]);
  const block = await within(rec.stop(), 500);
  check('a take with no failure stays "recorded"', block.cameraAudio?.state === 'recorded');
}
{
  // The picture alone (the camera has no microphone): an error ends the
  // picture, and the camera-audio reason is left as the browser gave it.
  const { rec, uploads, recorder } = start(null, { state: 'no-mic' });
  check('no sound in the recorder', recorder.stream.getAudioTracks().length === 0);
  recorder.data([5, 5, 5]);
  recorder.fail(new Error('video encoder failed'));
  recorder.fireStopOnStop = false;
  const block = await within(rec.stop(), 500);
  check('a picture-only recorder error keeps the picture', uploads.length === 1);
  check('and does not claim a sound failure', block.cameraAudio?.state === 'no-mic');
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
