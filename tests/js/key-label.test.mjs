// The merged key label (#736): "B minor", not "B min" plus "Natural Minor" plus
// a Scale card saying it a third time.
//
// Run:  node tests/js/key-label.test.mjs

import { formatKey } from '../../static/js/keyLabel.js';
import { TRANSLATIONS } from '../../static/js/i18n.js';

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

// The same lookup the app does: the language, then pt for pt-PT, then English.
const FALLBACK = { 'pt-PT': 'pt' };
function tFor(code) {
  return (key, vars = {}) => {
    let text;
    for (let c = code; c && text === undefined; c = FALLBACK[c]) text = TRANSLATIONS[c]?.[key];
    text ??= TRANSLATIONS.en[key] ?? key;
    return text.replace(/\{(\w+)\}/g, (_, v) => String(vars[v] ?? `{${v}}`));
  };
}
const en = tFor('en');

check('a major key reads as one label', formatKey('D maj', 'Major', en) === 'D major', formatKey('D maj', 'Major', en));
check('a natural minor key drops "natural"', formatKey('B min', 'Natural Minor', en) === 'B minor');
check('harmonic minor is named', formatKey('B min', 'Harmonic Minor', en) === 'B harmonic minor');
check('sharps keep their letter', formatKey('F# min', 'Natural Minor', en) === 'F# minor');
check('a track analysed before scale was stored still reads', formatKey('F# min', undefined, en) === 'F# minor');
check('an unknown scale falls back on the key', formatKey('C maj', 'Dorian', en) === 'C major');
check('an unreadable key is shown as stored', formatKey('weird', null, en) === 'weird');
check('no key gives nothing', formatKey(null, 'Major', en) === '' && formatKey('', null, en) === '');

// Every language, including the regional variant, produces a complete label:
// the tonic letter, no raw key names, no unfilled placeholder.
for (const code of Object.keys(TRANSLATIONS)) {
  const t = tFor(code);
  for (const scale of ['Major', 'Natural Minor', 'Harmonic Minor']) {
    const label = formatKey('F# min', scale, t);
    check(
      `${code}: ${scale} is a complete label`,
      label.startsWith('F#') && !/key\.|\{|\}/.test(label) && label.length > 3,
      label,
    );
  }
}

check('German reads naturally', formatKey('B min', 'Natural Minor', tFor('de')) === 'B Moll');
check('European Portuguese keeps its spelling', formatKey('B min', 'Harmonic Minor', tFor('pt-PT')) === 'B menor harmónica');

console.log(`\n${passed}/${passed + failed} checks passed`);
process.exit(failed === 0 ? 0 : 1);
