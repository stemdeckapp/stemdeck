// storeSetMany (static/js/utils.js, #996): several store keys in one write.
//
// On the desktop every store_set saves the whole user-data store to disk, so
// a version delete that cleared eight keys one by one rewrote the file eight
// times (386 to 626 ms on a 9 MB store, 47 to 83 ms for one save).
// storeSetMany sends every key in one store_set_many call, which saves once.
// Outside the desktop
// it writes each key to localStorage, and one key that cannot be written does
// not stop the others.

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
const stored = new Map();
globalThis.localStorage = {
  getItem: (k) => (stored.has(k) ? stored.get(k) : null),
  setItem: (k, v) => {
    if (k === 'full') throw new Error('quota exceeded');
    stored.set(k, String(v));
  },
};

const { storeSetMany } = await import('../../static/js/utils.js');

check('storeSetMany is exported', typeof storeSetMany === 'function');

// Desktop: one call, every key in it.
const calls = [];
window.__TAURI__ = { core: { invoke: async (cmd, args) => { calls.push({ cmd, args }); return null; } } };
await storeSetMany({ 'a:practice': null, 'a:daw': { voice: 'split' }, 'b:practice': 1 });
check('desktop: one invoke for three keys', calls.length === 1, JSON.stringify(calls));
check('desktop: the call is store_set_many', calls[0]?.cmd === 'store_set_many', calls[0]?.cmd);
check(
  'desktop: every key and value is in the call',
  JSON.stringify(calls[0]?.args) === JSON.stringify({ entries: { 'a:practice': null, 'a:daw': { voice: 'split' }, 'b:practice': 1 } }),
  JSON.stringify(calls[0]?.args),
);

// Desktop: nothing to write sends nothing.
calls.length = 0;
await storeSetMany({});
check('desktop: no keys, no call', calls.length === 0, JSON.stringify(calls));

// Desktop: a failed write is logged and does not throw.
window.__TAURI__ = { core: { invoke: async () => { throw new Error('disk full'); } } };
warnings.length = 0;
let threw = false;
try { await storeSetMany({ x: 1 }); } catch { threw = true; }
check('desktop: a failed write does not throw', !threw);
check('desktop: a failed write is logged', warnings.some((w) => w.includes('disk full')), JSON.stringify(warnings));

// Browser: each key in localStorage; one that fails is logged and the rest
// are still written.
delete window.__TAURI__;
warnings.length = 0;
await storeSetMany({ first: { a: 1 }, full: 2, last: null });
check('browser: the key before the failure is written', stored.get('first') === '{"a":1}', stored.get('first'));
check('browser: the key after the failure is written', stored.get('last') === 'null', stored.get('last'));
check('browser: the failure is logged with its key', warnings.some((w) => w.includes('full') && w.includes('quota')), JSON.stringify(warnings));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
