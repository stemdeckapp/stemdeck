// When the LUFS card says an older song was measured again (#923): only for a
// song the server measured again, with a basis it knows, and not once the
// note was dismissed for that loudness version.
//
// Run:  node tests/js/loudness-note.test.mjs

import { loudnessNoteKey } from '../../static/js/loudnessNote.js';

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

const stems = { remeasured: true, basis: 'stems', version: 2 };
const source = { remeasured: true, basis: 'source', version: 2 };

check('measured again from the stems', loudnessNoteKey(stems, undefined) === 'meta.loudnessNote.stems');
check('measured again from the kept upload', loudnessNoteKey(source, undefined) === 'meta.loudnessNote.source');
check('not measured again', loudnessNoteKey({ remeasured: false, basis: null, version: 2 }, undefined) === null);
check('no loudness at all', loudnessNoteKey(null, undefined) === null);
check('a server from before #923 sends no flag', loudnessNoteKey({ version: 2 }, undefined) === null);
check('an unknown basis shows nothing', loudnessNoteKey({ ...stems, basis: 'elsewhere' }, undefined) === null);
check('dismissed for this version', loudnessNoteKey(stems, 2) === null);
check('dismissed for an older version shows again', loudnessNoteKey({ ...stems, version: 3 }, 2) === 'meta.loudnessNote.stems');

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
