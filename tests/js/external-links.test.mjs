// A middle-click on an external link reaches the shell, not the webview (#1041).
import { externalLinkAction, isWebUrl, isLinkPress } from '../../static/js/externalLinks.js';

let passed = 0;
let failed = 0;
function check(name, condition, detail = '') {
  if (condition) { passed++; console.log(`PASS  ${name}`); }
  else { failed++; console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ''}`); }
}

// A minimal stand-in for an element: closest() answers for the anchors given.
function el({ ext, dl } = {}) {
  return { closest: (sel) => (sel === 'a[target="_blank"]' ? ext ?? null : sel === 'a.lane-dl' ? dl ?? null : null) };
}
const ext = (href) => ({ href });
const lane = (href, download, raw = href) => ({ href, download, getAttribute: () => raw });
const act = (o) => externalLinkAction({ hasShell: true, ...o });
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const U = 'https://stemdeck.app/';
check('left click opens through the shell', same(act({ type: 'click', button: 0, target: el({ ext: ext(U) }) }), { action: 'open', url: U }));
// Modifier keys are intentionally absent from the pure helper: Ctrl, Cmd and
// Shift left-clicks arrive as click with button 0 and take the plain path. (On
// macOS, Ctrl+click is a context menu, not a click, so it never reaches here.)
// The honest check is that an extra modifier field changes nothing.
check('modifier fields on the event do not change the decision', same(
  act({ type: 'click', button: 0, ctrlKey: true, metaKey: true, shiftKey: true, target: el({ ext: ext(U) }) }),
  act({ type: 'click', button: 0, target: el({ ext: ext(U) }) })));
check('middle auxclick opens through the shell', same(act({ type: 'auxclick', button: 1, target: el({ ext: ext(U) }) }), { action: 'open', url: U }));
check('right auxclick is left alone', act({ type: 'auxclick', button: 2, target: el({ ext: ext(U) }) }) === null);
check('auxclick button 0 is left alone', act({ type: 'auxclick', button: 0, target: el({ ext: ext(U) }) }) === null);
check('plain browser: nothing intercepted (click)', act({ hasShell: false, type: 'click', target: el({ ext: ext(U) }) }) === null);
check('plain browser: nothing intercepted (auxclick)', act({ hasShell: false, type: 'auxclick', button: 1, target: el({ ext: ext(U) }) }) === null);
check('http is accepted', same(act({ type: 'click', target: el({ ext: ext('http://example.com/a') }) }), { action: 'open', url: 'http://example.com/a' }));
check('mailto is blocked, never opened', same(act({ type: 'click', target: el({ ext: ext('mailto:a@b.c') }) }), { action: 'block' }));
check('javascript: is blocked', same(act({ type: 'auxclick', button: 1, target: el({ ext: ext('javascript:alert(1)') }) }), { action: 'block' }));
check('file: is blocked', same(act({ type: 'click', target: el({ ext: ext('file:///C:/x') }) }), { action: 'block' }));
check('a link that is not target=_blank is left alone', act({ type: 'click', target: el() }) === null);
check('a target with no closest is left alone', act({ type: 'click', target: {} }) === null);
check('stem download saves with its name', same(act({ type: 'click', target: el({ dl: lane('http://h/x.wav', 'Song - vocals.wav') }) }), { action: 'save', url: 'http://h/x.wav', filename: 'Song - vocals.wav' }));
check('stem download by middle-click saves too', act({ type: 'auxclick', button: 1, target: el({ dl: lane('http://h/x.wav', 'a.wav') }) })?.action === 'save');
check('placeholder lane link (href #) is not saved', act({ type: 'click', target: el({ dl: lane('http://h/#', 'a.wav', '#') }) })?.action === 'open');
check('lane link with no download name opens', same(act({ type: 'click', target: el({ dl: lane('http://h/x', '') }) }), { action: 'open', url: 'http://h/x' }));
check('isWebUrl', isWebUrl('https://a') && isWebUrl('HTTP://a') && !isWebUrl('ftp://a') && !isWebUrl(undefined));
check('isLinkPress', isLinkPress('click', 0) && isLinkPress('auxclick', 1) && !isLinkPress('auxclick', 2) && !isLinkPress('mousedown', 1));

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
