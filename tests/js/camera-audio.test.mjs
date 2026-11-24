// The camera's microphone for the picture's file: which input is the
// camera's, its constraints, the clone of an input already open, and every
// way opening it can end. A fake navigator stands in for the browser.
//
// Run:  node tests/js/camera-audio.test.mjs

import {
  matchCameraMic, cameraMicConstraints, micErrorState, openTrackOn, openCameraAudio, openDecision,
} from '../../static/js/video/cameraAudio.js';
import {
  pickRecorderMime, MIME_LADDER_AV, buildVideoBlock, cameraAudioState, CAMERA_AUDIO_STATES,
} from '../../static/js/video/videoCodec.js';

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

const DEVICES = [
  { kind: 'videoinput', deviceId: 'cam1', groupId: 'g-cam', label: 'Logitech StreamCam' },
  { kind: 'audioinput', deviceId: 'default', groupId: 'g-cam', label: 'Default - StreamCam' },
  { kind: 'audioinput', deviceId: 'mic-cam', groupId: 'g-cam', label: 'Microphone (StreamCam)' },
  { kind: 'audioinput', deviceId: 'mic-usb', groupId: 'g-usb', label: 'Line (MG-XU)' },
  { kind: 'audiooutput', deviceId: 'out', groupId: 'g-cam', label: 'Speakers' },
];

// ── Which microphone ──
check('the camera microphone is the audio input of the camera group',
  matchCameraMic('g-cam', DEVICES)?.deviceId === 'mic-cam');
check('the real entry wins over the Windows "default" entry of the same device',
  matchCameraMic('g-cam', DEVICES)?.deviceId !== 'default');
check('only the virtual entry: it is used',
  matchCameraMic('g-cam', DEVICES.filter((d) => d.deviceId !== 'mic-cam'))?.deviceId === 'default');
check('a camera with no microphone: none',
  matchCameraMic('g-other', DEVICES) === null);
check('no group id (an engine that hides it): none',
  matchCameraMic('', DEVICES) === null && matchCameraMic(undefined, DEVICES) === null);
check('inputs with no device id (no microphone permission yet): none',
  matchCameraMic('g-cam', [{ kind: 'audioinput', deviceId: '', groupId: 'g-cam' }]) === null);
check('never an output', matchCameraMic('g-cam', [DEVICES[4]]) === null);

// ── Constraints: the take's own capture settings ──
const c = cameraMicConstraints('mic-cam');
check('the exact device', c.deviceId.exact === 'mic-cam');
check('no echo cancellation, noise suppression or gain control',
  c.echoCancellation === false && c.noiseSuppression === false && c.autoGainControl === false);

check('a refusal is "denied"', micErrorState({ name: 'NotAllowedError' }) === 'denied'
  && micErrorState({ name: 'SecurityError' }) === 'denied');
check('anything else is "failed"', micErrorState({ name: 'NotReadableError' }) === 'failed'
  && micErrorState(null) === 'failed');

// ── An input already open ──
function fakeTrack(deviceId, readyState = 'live') {
  const t = {
    kind: 'audio', readyState, stopped: false, cloned: 0,
    getSettings: () => ({ deviceId }),
    clone() { this.cloned++; return fakeTrack(deviceId); },
    stop() { this.stopped = true; },
  };
  return t;
}
const stream = (...tracks) => ({ getAudioTracks: () => tracks });
const open = fakeTrack('mic-cam');
check('the take input on the camera microphone is found',
  openTrackOn(DEVICES[2], [stream(fakeTrack('mic-usb')), stream(open)]) === open);
check('an ended track is not', openTrackOn(DEVICES[2], [stream(fakeTrack('mic-cam', 'ended'))]) === null);
check('another device is not', openTrackOn(DEVICES[2], [stream(fakeTrack('mic-usb'))]) === null);
// The take input opened as Windows' "default" entry of the camera microphone.
const asDefault = fakeTrack('default');
check('the take input opened as "default" of the same group is found through the device list',
  openTrackOn(DEVICES[2], [stream(asDefault)], DEVICES) === asDefault);
check('"default" of another group is not',
  openTrackOn(DEVICES[2], [stream(fakeTrack('default'))], DEVICES.map((d) => (d.deviceId === 'default' ? { ...d, groupId: 'g-usb' } : d))) === null);
const byGroup = { ...fakeTrack('some-id'), getSettings: () => ({ deviceId: 'some-id', groupId: 'g-cam' }) };
check("a track reporting the camera microphone's group id is found", openTrackOn(DEVICES[2], [stream(byGroup)], []) === byGroup);

// ── May it open while a take starts (never with a prompt) ──
const named = { label: 'Microphone (StreamCam)' };
check('granted: open', openDecision('granted', named) === 'open' && openDecision('granted', {}) === 'open');
check('prompt: not asked, no prompt mid-take', openDecision('prompt', named) === 'not-asked');
check('denied: denied', openDecision('denied', named) === 'denied');
check('no Permissions API, the device list names the microphone (access granted): open',
  openDecision('unknown', named) === 'open');
check('no Permissions API, no name (access not granted): not asked', openDecision('unknown', { label: '' }) === 'not-asked');

// ── Opening ──
function setNavigator(nav) {
  Object.defineProperty(globalThis, 'navigator', { value: nav, configurable: true, writable: true });
}
const camTrack = { getSettings: () => ({ deviceId: 'cam1', groupId: 'g-cam' }) };

function fakeNavigator({ permission = 'granted', gum, devices = DEVICES } = {}) {
  const calls = [];
  return {
    calls,
    permissions: permission === null ? undefined : { query: async () => ({ state: permission }) },
    mediaDevices: {
      enumerateDevices: async () => devices,
      getUserMedia: async (constraints) => {
        calls.push(constraints);
        return gum ? gum(constraints) : { getAudioTracks: () => [fakeTrack(constraints.audio.deviceId.exact)] };
      },
    },
  };
}

{
  const nav = fakeNavigator();
  setNavigator(nav);
  const r = await openCameraAudio(camTrack);
  check('opens the camera microphone: "recorded" with a track and its label',
    r.state === 'recorded' && r.track && r.label === 'Microphone (StreamCam)');
  check('with the camera microphone constraints, audio only',
    nav.calls.length === 1 && nav.calls[0].audio.deviceId.exact === 'mic-cam' && nav.calls[0].video === false
      && nav.calls[0].audio.echoCancellation === false);
}
{
  const nav = fakeNavigator();
  setNavigator(nav);
  const input = fakeTrack('mic-cam');
  const r = await openCameraAudio(camTrack, [stream(input)]);
  check('the take input on the same microphone is cloned, no second open',
    r.state === 'recorded' && input.cloned === 1 && nav.calls.length === 0);
  check('the clone is not the input (stopping it leaves the take input alone)', r.track !== input);
}
{
  setNavigator(fakeNavigator());
  const noGroup = { getSettings: () => ({ deviceId: 'cam1' }) };
  const r = await openCameraAudio(noGroup);
  check('a track without a group id is matched through the device list', r.state === 'recorded');
}
{
  setNavigator(fakeNavigator({ devices: DEVICES.filter((d) => d.groupId !== 'g-cam' || d.kind === 'videoinput') }));
  const r = await openCameraAudio(camTrack);
  check('no microphone in the camera: "no-mic", no track', r.state === 'no-mic' && r.track === null);
}
{
  const nav = fakeNavigator();
  setNavigator(nav);
  const input = fakeTrack('default');
  const r = await openCameraAudio(camTrack, [stream(input)]);
  check('the take input opened as "default" is cloned, never opened a second time',
    r.state === 'recorded' && input.cloned === 1 && nav.calls.length === 0);
}
{
  const nav = fakeNavigator({ permission: 'prompt' });
  setNavigator(nav);
  const r = await openCameraAudio(camTrack);
  check('permission not asked yet: "not-asked", and no prompt while the take starts',
    r.state === 'not-asked' && r.track === null && nav.calls.length === 0);
}
{
  const nav = fakeNavigator({ permission: 'denied' });
  setNavigator(nav);
  const r = await openCameraAudio(camTrack);
  check('permission refused: "denied", nothing opened', r.state === 'denied' && nav.calls.length === 0);
}
{
  setNavigator(fakeNavigator({ permission: null }));
  const r = await openCameraAudio(camTrack);
  check('no Permissions API, microphone named in the list: opened', r.state === 'recorded');
}
{
  const nav = fakeNavigator({ permission: null, devices: DEVICES.map((d) => ({ ...d, label: '' })) });
  setNavigator(nav);
  const r = await openCameraAudio(camTrack);
  check('no Permissions API and no names: "not-asked", getUserMedia never called',
    r.state === 'not-asked' && nav.calls.length === 0);
}
{
  setNavigator(fakeNavigator({ gum: () => { throw Object.assign(new Error('no'), { name: 'NotAllowedError' }); } }));
  const r = await openCameraAudio(camTrack);
  check('refused at open: "denied"', r.state === 'denied' && r.track === null);
}
{
  setNavigator(fakeNavigator({ gum: () => { throw Object.assign(new Error('busy'), { name: 'NotReadableError' }); } }));
  const r = await openCameraAudio(camTrack);
  check('a busy device: "failed"', r.state === 'failed' && r.track === null);
}
{
  let late = null;
  setNavigator(fakeNavigator({
    gum: (cons) => new Promise((resolve) => setTimeout(() => {
      late = fakeTrack(cons.audio.deviceId.exact);
      resolve({ getAudioTracks: () => [late] });
    }, 80)),
  }));
  const r = await openCameraAudio(camTrack, [], 20);
  check('an open that takes too long: "failed", the take goes on', r.state === 'failed' && r.track === null);
  await new Promise((res) => setTimeout(res, 120));
  check('the microphone that opened late is stopped', late?.stopped === true);
}
{
  setNavigator({});
  const r = await openCameraAudio(camTrack);
  check('no mediaDevices: "unsupported"', r.state === 'unsupported');
  check('no camera track: "unsupported"', (await openCameraAudio(null)).state === 'unsupported');
}

// ── The recorder's type, with the sound ──
const only = (...ok) => (type) => ok.includes(type);
check('MP4 H.264 + AAC first',
  pickRecorderMime(only(...MIME_LADDER_AV), true) === 'video/mp4;codecs=avc1.42E01E,mp4a.40.2');
check('WebM VP9 + Opus on a WebM engine',
  pickRecorderMime(only('video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm'), true)
    === 'video/webm;codecs=vp9,opus');
check('a plain container type last (the engine adds its sound codec)',
  pickRecorderMime(only('video/webm'), true) === 'video/webm');
check('the picture-only ladder is unchanged without the sound',
  pickRecorderMime(only('video/mp4;codecs=avc1.42E01E', 'video/mp4;codecs=avc1.42E01E,mp4a.40.2'))
    === 'video/mp4;codecs=avc1.42E01E');

// ── The video block says what went into the file ──
const base = { mime: 'video/mp4', ext: 'mp4', parts: 1, perfMsAtFrame0: 100, startedAtPerf: 150 };
check('"recorded" with its label',
  JSON.stringify(buildVideoBlock({ ...base, cameraAudio: { state: 'recorded', label: 'Cam' } }).cameraAudio)
    === JSON.stringify({ state: 'recorded', label: 'Cam' }));
check('a reason without a label', JSON.stringify(buildVideoBlock({ ...base, cameraAudio: { state: 'no-mic' } }).cameraAudio)
  === JSON.stringify({ state: 'no-mic' }));
check('nothing known: no cameraAudio key (an older take, the studio camera)',
  !('cameraAudio' in buildVideoBlock(base)) && !('cameraAudio' in buildVideoBlock({ ...base, cameraAudio: { state: 'odd' } })));
check('the states are the server\'s',
  CAMERA_AUDIO_STATES.join() === 'recorded,no-mic,denied,not-asked,failed,unsupported' && cameraAudioState('x') === null);
check('a long label is cut to 200',
  buildVideoBlock({ ...base, cameraAudio: { state: 'recorded', label: 'x'.repeat(300) } }).cameraAudio.label.length === 200);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
