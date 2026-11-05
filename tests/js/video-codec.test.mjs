// Pure helpers behind the camera take: the codec ladder, part cutting, the
// nudge clamp and the take.json "video" block the server validates.
//
// Run:  node tests/js/video-codec.test.mjs

import {
  pickRecorderMime, extForMime, cutParts, PART_BYTES, clampOffsetMs, buildVideoBlock,
  cameraConstraints, takeHasVideo, MIME_LADDER, firstRecordedCapture, AUDIO_STAMP_LAG_MS,
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

// ── Codec ladder ──
const only = (...ok) => (type) => ok.includes(type);
check('MP4 H.264 wins where the engine has it (Safari, recent Chromium)',
  pickRecorderMime(only('video/mp4;codecs=avc1.42E01E', 'video/webm;codecs=vp9')) === 'video/mp4;codecs=avc1.42E01E');
check('plain video/mp4 when no codec string is accepted (older Safari)',
  pickRecorderMime(only('video/mp4')) === 'video/mp4');
check('VP9 before VP8 on a WebM-only engine',
  pickRecorderMime(only('video/webm;codecs=vp8', 'video/webm;codecs=vp9')) === 'video/webm;codecs=vp9');
check('VP8 when that is all there is',
  pickRecorderMime(only('video/webm;codecs=vp8')) === 'video/webm;codecs=vp8');
check('empty string when nothing is supported, so the recorder picks its own',
  pickRecorderMime(() => false) === '');
check('no isTypeSupported at all (MediaRecorder missing) gives empty, not a throw',
  pickRecorderMime(undefined) === '');
check('a throwing isTypeSupported is skipped, not fatal',
  pickRecorderMime((t) => { if (t.startsWith('video/mp4')) throw new Error('x'); return t === 'video/webm;codecs=vp8'; }) === 'video/webm;codecs=vp8');
check('ladder asks MP4 first', MIME_LADDER[0].startsWith('video/mp4'));

check('mp4 extension', extForMime('video/mp4;codecs=avc1') === 'mp4');
check('webm extension', extForMime('video/webm;codecs=vp9') === 'webm');
check('unknown type falls back to webm', extForMime('') === 'webm');

// ── Constraints: 720p default, 1080p ceiling, 30 fps ──
const c720 = cameraConstraints('720', '');
check('720p ideal is 1280x720', c720.width.ideal === 1280 && c720.height.ideal === 720);
check('maximum is 1920x1080 at 30 fps', c720.width.max === 1920 && c720.height.max === 1080 && c720.frameRate.max === 30);
check('no device pinned when none is chosen', c720.deviceId === undefined);
const c1080 = cameraConstraints('1080', 'cam-1');
check('1080p ideal is 1920x1080', c1080.width.ideal === 1920 && c1080.height.ideal === 1080);
check('a chosen device is pinned exactly', c1080.deviceId?.exact === 'cam-1');
check('unknown quality falls back to 720p', cameraConstraints('4k').width.ideal === 1280);

// ── Part cutting ──
// A Blob-shaped fake: size and slice are all cutParts uses.
function fake(size) {
  return { size, slice: (a, b = size) => fake(Math.max(0, Math.min(size, b) - a)) };
}
let r = cutParts(fake(100), false, 40);
check('full parts are cut and the rest is kept', r.parts.length === 2 && r.parts.every((p) => p.size === 40) && r.rest.size === 20);
r = cutParts(fake(100), true, 40);
check('final flush sends the remainder as a last part', r.parts.length === 3 && r.parts[2].size === 20 && r.rest === null);
r = cutParts(fake(30), false, 40);
check('under one part nothing is sent mid-take', r.parts.length === 0 && r.rest.size === 30);
r = cutParts(fake(0), true, 40);
check('an empty final flush sends nothing', r.parts.length === 0);
r = cutParts(fake(80), true, 40);
check('an exact multiple leaves no empty tail part', r.parts.length === 2);
check('part size is the server body cap, 4 MiB', PART_BYTES === 4 * 1024 * 1024);

// ── Nudge clamp ──
check('rounds to whole ms', clampOffsetMs('33.6') === 34);
check('clamps high', clampOffsetMs(99999) === 1000);
check('clamps low', clampOffsetMs(-99999) === -1000);
check('garbage is 0', clampOffsetMs('abc') === 0 && clampOffsetMs(NaN) === 0);

// ── take.json video block ──
const block = buildVideoBlock({
  mime: 'video/webm;codecs=vp9', width: 1280, height: 720, fps: 30, deviceLabel: 'Fake cam',
  mirrorPreview: true, perfMsAtFrame0: 1000, ctxTimeAtFrame0: 2.5, startedAtPerf: 1120.25,
  firstChunkAtPerf: 1120.25, parts: 3,
});
check('file name follows the type', block.file === 'camera.webm');
check('firstFrameAtMs is camera start minus audio frame 0 (positive = later)', block.firstFrameAtMs === 120.25, String(block.firstFrameAtMs));
check('dimensions and fps carried', block.w === 1280 && block.h === 720 && block.fps_nominal === 30);
check('default offset is 0', block.cameraOffsetMs === 0);
check('parts counted', block.parts === 3);
check('no null anywhere (the server rejects non-finite numbers)', !Object.values(block).some((v) => v === null || (typeof v === 'number' && !Number.isFinite(v))));

const sparse = buildVideoBlock({ mime: '', startedAtPerf: 50, parts: 0 });
check('unknown values are left out, not sent as null',
  !('w' in sparse) && !('h' in sparse) && !('firstFrameAtMs' in sparse) && !('startedAtAudioCtx' in sparse));
check('missing fps falls back to 30', sparse.fps_nominal === 30);
check('device label is capped at 200 chars', buildVideoBlock({ deviceLabel: 'x'.repeat(500) }).deviceLabel.length === 200);
check('an absurd width is capped at 4096', buildVideoBlock({ width: 99999 }).w === 4096);

// ── First recorded frame and its clock ──
// Frames every 33.3 ms; the recorder starts at 1000: its first frame is the
// first one captured after that.
const caps = [933.3, 966.6, 999.9, 1033.2, 1066.5, 1099.8];
check('first frame captured after the start call', firstRecordedCapture(caps, 1000) === 1033.2);
check('a frame the preview skipped is put back',
  Math.abs(firstRecordedCapture([933.3, 966.6, 999.9, 1066.5, 1099.8, 1133.1], 1000.5) - 1033.2) < 0.01);
check('nothing after the call: no time', firstRecordedCapture([900, 933], 1000) === null);
check('no capture times: no time', firstRecordedCapture([], 1000) === null && firstRecordedCapture(null, 1000) === null);
check('jitter is not a skipped frame', firstRecordedCapture([966.6, 999.0, 1036.0, 1069.3], 1001) === 1036.0);
const captured = buildVideoBlock({ mime: 'video/mp4', perfMsAtFrame0: 1000, startedAtPerf: 1060, firstCapturePerf: 1033.2 });
check('with a capture time, the block uses it on the audio stamp clock',
  captured.firstFrameAtMs === Math.round((33.2 + AUDIO_STAMP_LAG_MS) * 1000) / 1000 && captured.firstFrameClock === 'capture',
  JSON.stringify(captured));
check('the audio stamp lag is the measured 30 ms', AUDIO_STAMP_LAG_MS === 30);
check('without one, the recorder start stands in and says so',
  block.firstFrameClock === 'recorder-start' && block.firstFrameAtMs === 120.25);

// ── Which takes get "Make video" ──
check('has_camera true shows it', takeHasVideo({ id: 't00000001', has_camera: true }));
check('has_camera false hides it', !takeHasVideo({ id: 't00000001', has_camera: false }));
check('a take without the field hides it', !takeHasVideo({ id: 't00000001' }) && !takeHasVideo(null));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
