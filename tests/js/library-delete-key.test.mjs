// Delete and Backspace move a song to the Trash only from the library's rows.
//
// They used to trash the open song from anywhere: with focus on Play, a mute
// button, a stem switch or a Karaoke queue row, one key press sent the song
// to the Trash with no confirmation.
import { libraryDeleteTrackId, isTextEditingTarget, ignoredByShortcuts } from '../../static/js/libraryDeleteKey.js';

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

/**
 * A stand-in element: `closest` walks a chain of { matches } ancestors, each
 * matching a list of selector fragments (enough for the selectors in use).
 */
function el(tags, parent = null, extra = {}) {
  const node = {
    tags, parent, ...extra,
    classList: { contains: (c) => tags.includes(`.${c}`) },
    closest(sel) {
      const parts = sel.split(',').map((p) => p.trim());
      for (let n = this; n; n = n.parent) {
        if (parts.some((p) => matches(n, p))) return n;
      }
      return null;
    },
  };
  return node;
}
function matches(n, sel) {
  // "#id", ".class", "tag", "[attr]", ".a[data-id]": every piece must be in tags.
  const pieces = sel.match(/[#.]?[\w-]+(?:=[^\]]+)?|\[[^\]]+\]/g) || [];
  return pieces.every((p) => n.tags.includes(p));
}

const body = el(['body']);
const root = el(['html']);
body.parent = root;
const panel = el(['#catalogPanel'], body);
const list = el(['#catalogList'], panel);
const row = el(['.cat-item', '[data-id]'], list, { dataset: { id: 'songB' } });
const rowDel = el(['button'], row);
const trashRow = el(['.cat-item', '.in-trash', '[data-id]'], list, { dataset: { id: 'songT' } });
const queueRow = el(['.cat-item', '.queue-row', '[data-id]'], list, { dataset: { id: 'job9' } });
const rail = el(['.sidebar-rail'], panel);
const modeBtn = el(['button', '#modeBtn'], rail);
const search = el(['input', '#catalogSearch'], panel);
const player = el(['.daw-main-col'], body);
const play = el(['button', '#playBtn'], player);
const mute = el(['button', '.lane-mute'], player);
const wave = el(['.stem-waveform-row'], player);
const dialog = el(['[role=dialog]', '.settings-overlay'], body);
const settingsBtn = el(['button'], dialog);
const karaokeQueueRow = el(['.kq-row'], el(['#karaoke'], body));
const editable = el(['div'], body, { isContentEditable: true });

const key = (target, k = 'Delete', extra = {}) => ({ key: k, target, defaultPrevented: false, ...extra });
const opts = (o = {}) => ({ openTrackId: 'songA', lastPointerInList: false, body, root, ...o });

// Where it still trashes.
check('Delete on a row trashes that row', libraryDeleteTrackId(key(rowDel), opts()) === 'songB');
check('Backspace on a row trashes that row', libraryDeleteTrackId(key(rowDel, 'Backspace'), opts()) === 'songB');
check('inside the list, off a row, it is the open song', libraryDeleteTrackId(key(list), opts()) === 'songA');
check('on the body after a click in the list, the open song',
  libraryDeleteTrackId(key(body), opts({ lastPointerInList: true })) === 'songA');
check('on the root after a click in the list, the open song',
  libraryDeleteTrackId(key(root), opts({ lastPointerInList: true })) === 'songA');

// Where it never does.
for (const [name, target] of [
  ['Play', play], ['a mute button', mute], ['a waveform row', wave], ['the Mode button', modeBtn],
  ['the Settings dialog', settingsBtn], ['a Karaoke queue row', karaokeQueueRow],
]) {
  check(`Delete on ${name} does nothing`, libraryDeleteTrackId(key(target), opts()) === null);
  check(`... even after a click in the list`, libraryDeleteTrackId(key(target), opts({ lastPointerInList: true })) === null);
}
check('on the body with no click in the list, nothing', libraryDeleteTrackId(key(body), opts()) === null);
check('a key already handled is left alone',
  libraryDeleteTrackId(key(rowDel, 'Delete', { defaultPrevented: true }), opts()) === null);
check('typing in the search box is typing', libraryDeleteTrackId(key(search, 'Backspace'), opts()) === null);
check('contenteditable is typing', libraryDeleteTrackId(key(editable, 'Backspace'), opts({ lastPointerInList: true })) === null);
check('isTextEditingTarget sees an input', isTextEditingTarget(search) === true);
check('a Trash row is not trashed again', libraryDeleteTrackId(key(trashRow), opts()) === null);
check('a queue row is not a song to trash', libraryDeleteTrackId(key(queueRow), opts()) === null);
check('other keys do nothing', libraryDeleteTrackId(key(rowDel, 'x'), opts()) === null);
check('no open song, nothing from the body', libraryDeleteTrackId(key(body), opts({ openTrackId: null, lastPointerInList: true })) === null);

// The one-key shortcuts (Space, [, ], L, K, I, O) leave browser and OS chords
// and keys meant for a focused field alone. Ctrl+L, Cmd+K and Alt+[ used to
// be swallowed, and Space on a select played the song instead of opening it.
const press = (target, mods = {}) => ({ code: 'Space', target, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...mods });
check('a plain key on the page is a shortcut', ignoredByShortcuts(press(body)) === false);
check('a plain key on a button is a shortcut', ignoredByShortcuts(press(play)) === false);
check('Shift alone keeps the shortcut', ignoredByShortcuts(press(body, { shiftKey: true })) === false);
for (const mod of ['ctrlKey', 'metaKey', 'altKey']) {
  check(`${mod} leaves the key to the browser`, ignoredByShortcuts(press(body, { [mod]: true })) === true);
  check(`${mod} with Shift too`, ignoredByShortcuts(press(play, { [mod]: true, shiftKey: true })) === true);
}
check('Ctrl and Alt together (AltGr)', ignoredByShortcuts(press(body, { ctrlKey: true, altKey: true })) === true);
for (const tag of ['input', 'textarea', 'select']) {
  check(`a focused ${tag} keeps its keys`, ignoredByShortcuts(press(el([tag], player))) === true);
}
check('contenteditable keeps its keys', ignoredByShortcuts(press(editable)) === true);
check('a child of contenteditable keeps its keys',
  ignoredByShortcuts(press(el(['span'], el(['div', "[contenteditable='true']"], body)))) === true);
check('the folder name editor keeps its keys', ignoredByShortcuts(press(el(['.folder-editor'], body))) === true);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
