// storeGet (static/js/utils.js, #1023) while the shell cannot open the store.
//
// The shell answers "user-data-unavailable: ..." while user-data.json is there
// but cannot be read (a virus scanner or a sync client holding it). Taking the
// fallback at once would show an empty library for a moment's lock, so the
// read asks again for about four seconds first. Any other error falls back at
// once, as before.
//
// Run:  node tests/js/store-get-unavailable.test.mjs

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

const warnings = [];
console.warn = (...args) => warnings.push(args.map(String).join(' '));
globalThis.window = {};
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const { storeGet } = await import('../../static/js/utils.js');

// Unavailable twice, then the real value: the page gets the real value.
let calls = 0;
window.__TAURI__ = {
  core: {
    invoke: async () => {
      calls++;
      if (calls <= 2) throw 'user-data-unavailable: user-data.json could not be read';
      return { folders: ['Rock'] };
    },
  },
};
let started = Date.now();
let got = await storeGet('stemdeck.folders', { folders: [] });
check('a read that works on the third try gets the real value', JSON.stringify(got) === '{"folders":["Rock"]}', JSON.stringify(got));
check('three tries', calls === 3, String(calls));
check('waited between tries', Date.now() - started >= 700, `${Date.now() - started} ms`);
check('nothing logged for a lock that cleared', warnings.length === 0, warnings.join(' | '));

// Unavailable throughout: the fallback after five tries, and a warning.
calls = 0;
window.__TAURI__ = {
  core: { invoke: async () => { calls++; throw 'user-data-unavailable: still locked'; } },
};
got = await storeGet('stemdeck.folders', 'fallback');
check('unavailable throughout falls back', got === 'fallback', String(got));
check('five tries', calls === 5, String(calls));
check('the fallback is logged', warnings.length === 1, warnings.join(' | '));

// Any other error falls back at once.
calls = 0;
warnings.length = 0;
window.__TAURI__ = {
  core: { invoke: async () => { calls++; throw 'failed to create jobs folder'; } },
};
started = Date.now();
got = await storeGet('k', 'fallback');
check('another error falls back at once', got === 'fallback' && calls === 1, `${got} after ${calls}`);
check('no wait for another error', Date.now() - started < 200, `${Date.now() - started} ms`);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
