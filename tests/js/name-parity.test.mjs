// JS half of the name parity gate: the page's lyrics matching
// (static/js/lyricsLookup.js, zhVariants.js) held to what the server's
// (app/pipeline/lyrics_lookup.py, name_aliases.py, zh_variants.py) says.
//
// tests/fixtures/name_parity.json is generated from Python and asserted by
// tests/test_name_parity.py; this file asserts the page gives the same
// answers. A version the server kept and the page refuses (or the other way
// round) is a Lyrics tab that disagrees with the import.
//
// The traditional-to-simplified table is also checked character by character
// against app/_vendor/opencc/zh_t2s.txt, read the way zh_variants.py reads it,
// since the CI job running this file has no Python to ask.
//
// Run:  node tests/js/name-parity.test.mjs

import { readFileSync } from 'node:fs';
import { fold, scriptNames, sameArtist, sameSong } from '../../static/js/lyricsLookup.js';
import { PAIRS, toSimplified } from '../../static/js/zhVariants.js';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/name_parity.json', import.meta.url), 'utf8'));

let passed = 0;
let failed = 0;

function check(name, condition, detail = '') {
  if (condition) {
    passed++;
  } else {
    failed++;
    console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ''}`);
  }
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

check('the fixture is not empty', fixture.fold.length >= 10 && fixture.sameArtist.length >= 20 && fixture.sameSong.length >= 10);

for (const { in: text, out } of fixture.fold) {
  const got = fold(text);
  check(`fold(${JSON.stringify(text)})`, got === out, `page ${JSON.stringify(got)}, server ${JSON.stringify(out)}`);
}
for (const { in: name, out } of fixture.scriptNames) {
  const got = scriptNames(name);
  check(`scriptNames(${JSON.stringify(name)})`, same(got, out), `page ${JSON.stringify(got)}, server ${JSON.stringify(out)}`);
}
for (const { found, names, same: want } of fixture.sameArtist) {
  const got = sameArtist(found, names);
  check(`sameArtist(${JSON.stringify(found)}, ${JSON.stringify(names)})`, got === want, `page ${got}, server ${want}`);
}
for (const { found, song, same: want } of fixture.sameSong) {
  const got = sameSong(found, song);
  check(`sameSong(${JSON.stringify(found)}, ${JSON.stringify(song)})`, got === want, `page ${got}, server ${want}`);
}

// ─── zhVariants.js against the data file zh_variants.py reads ───

const data = readFileSync(new URL('../../app/_vendor/opencc/zh_t2s.txt', import.meta.url), 'utf8');
const server = new Map();
for (const line of data.split(/\r\n|\n|\r/)) {
  if (line.startsWith('#')) continue;
  const pair = line.split(' ');
  // Python's len() counts code points, as Array.from does.
  if (pair.length === 2 && Array.from(pair[0]).length === 1 && Array.from(pair[1]).length === 1) {
    server.set(pair[0], pair[1]);
  }
}
const page = new Map();
const chars = Array.from(PAIRS);
check('PAIRS is whole pairs', chars.length % 2 === 0, String(chars.length));
for (let i = 0; i + 1 < chars.length; i += 2) page.set(chars[i], chars[i + 1]);
check('the page folds as many characters as the server', page.size === server.size, `page ${page.size}, server ${server.size}`);

let wrong = [];
for (const [traditional, simplified] of server) {
  if (toSimplified(traditional) !== simplified) wrong.push(`${traditional}->${toSimplified(traditional)} (want ${simplified})`);
}
check('every character the server folds, the page folds the same way', !wrong.length, wrong.slice(0, 5).join(', '));

// Astral-plane characters (U+20000 up) are two UTF-16 units: folded whole.
const astral = [...server.keys()].filter((ch) => ch.codePointAt(0) > 0xffff);
check('the table has astral characters to fold', astral.length > 0);
wrong = astral.filter((ch) => toSimplified(`a${ch}b`) !== `a${server.get(ch)}b`);
check('an astral character folds in the middle of text', !wrong.length, wrong.slice(0, 3).join(', '));
check('text with nothing to fold is left as it is', toSimplified('宇多田ヒカル, 아이유, Café') === '宇多田ヒカル, 아이유, Café');
check('nothing is nothing', toSimplified(undefined) === '' && toSimplified(null) === '');

console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
