// main.js drove `multitrack` directly while audioEngine owns the clock (#515).
//
// engineMode() defaults to "chunked", where the multitrack is mounted with
// url: null for visuals only. So the footer scrub bar did nothing at all, and
// "set loop in at playhead" always wrote 0 because multitrack.getCurrentTime()
// is pinned there.
//
// A structural check rather than a DOM one: the bug was not a wrong value from
// a function, it was reaching for the wrong object. Anything that reintroduces
// a bare multitrack playback call in main.js fails here.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const mainSrc = readFileSync(join(root, 'static/js/main.js'), 'utf8');
const transportSrc = readFileSync(join(root, 'static/js/transport.js'), 'utf8');

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

// Strip comments so the explanatory ones below don't count as usage.
const code = mainSrc.replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');

for (const call of ['multitrack.setTime', 'multitrack.getCurrentTime', 'multitrack.getDuration']) {
  check(
    `main.js does not call ${call}`,
    !code.includes(call),
    'the multitrack is silent on the default chunked engine',
  );
}

check(
  'transport.js exports the accessor',
  /export function transport\(\)/.test(transportSrc),
);

check(
  'the accessor prefers the audio engine',
  /return audioEngine \?\? multitrack;/.test(transportSrc),
);

check(
  'transport.js exports setPlayheadTime',
  /export function setPlayheadTime\(/.test(transportSrc),
);

check(
  'main.js imports both rather than re-deriving them',
  /import \{[^}]*\btransport\b[^}]*\} from "\.\/transport\.js"/.test(mainSrc) &&
    /import \{[^}]*\bsetPlayheadTime\b[^}]*\} from "\.\/transport\.js"/.test(mainSrc),
);

check(
  'the keyboard guard excludes textareas',
  code.includes('HTMLTextAreaElement'),
  'Space in the log viewer used to start playback instead of scrolling',
);

console.log(`\n${passed}/${passed + failed} checks passed`);
process.exit(failed === 0 ? 0 : 1);
