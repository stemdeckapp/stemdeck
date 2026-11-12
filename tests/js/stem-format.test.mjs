// Songs stored as MP3 (#783): which engine they open on, and the name a lane
// download is saved under (playbackStems.js).
import { hasCompressedStems, stemFileExt } from '../../static/js/playbackStems.js';

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

const job = '/api/jobs/abcdef123456';
const wav = (name) => ({ name, url: `${job}/stems/${name}.wav` });
const mp3 = (name) => ({ name, url: `${job}/stems/${name}.mp3` });

check('a WAV song streams (chunked engine)', hasCompressedStems([wav('vocals'), wav('drums')]) === false);
check('an MP3 song decodes in full', hasCompressedStems([mp3('vocals'), mp3('drums')]) === true);
check(
  'an MP3 song with a WAV take still decodes in full',
  hasCompressedStems([mp3('vocals'), { name: 't0123abcd', url: `${job}/takes/t0123abcd/audio.wav` }]) === true,
);
check('a query string does not hide the extension', hasCompressedStems([{ url: `${mp3('bass').url}?v=2` }]) === true);
check('a name that only contains mp3 is not one', hasCompressedStems([{ url: `${job}/stems/mp3.wav` }]) === false);
check('no stems, no URLs', hasCompressedStems([]) === false && hasCompressedStems([{ name: 'x' }]) === false);
check('null is not a list of stems', hasCompressedStems(null) === false);

check('an MP3 stem downloads as .mp3', stemFileExt(mp3('vocals').url) === 'mp3');
check('a WAV stem downloads as .wav', stemFileExt(wav('vocals').url) === 'wav');
check('upper case counts', stemFileExt('/a/B.MP3') === 'mp3');
check('no URL falls back to wav, as before', stemFileExt(undefined) === 'wav');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
