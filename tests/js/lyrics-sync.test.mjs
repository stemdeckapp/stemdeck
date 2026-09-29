// Sync lines (lyricsSync.js): which timing of a track's lyrics is in effect,
// how a line moved or tapped moves its words and never crosses its
// neighbours, the tap sequence and its undo, snapping to the singing, and the
// LRC that is kept.
//
// Run:  node tests/js/lyrics-sync.test.mjs

import {
  timingOf,
  formatStamp,
  serializeLrc,
  fitWords,
  applyTime,
  snapToVoice,
  nextSungIndex,
  SyncSession,
  MIN_GAP_SEC,
} from "../../static/js/lyricsSync.js";
import { parseLrc, fromServerLyrics } from "../../static/js/lyricsLookup.js";

let pass = 0;
let fail = 0;
const check = (name, condition, detail = "") => {
  if (condition) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? `\n      ${detail}` : ""}`);
  }
};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;
const times = (lines) => lines.map((l) => Math.round(l.time * 100) / 100);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const OWN = "[00:00.00]\n[00:01.00] One line\n[00:03.00] Two line\n[00:05.00] Three line";
const ALIGNED = "[00:01.20]<00:01.20>One <00:01.60>line\n[00:03.10] Two line\n[00:05.40] Three line\n[00:00.00]";
const USER = "[00:00.00]\n[00:01.50] One line\n[00:03.50] Two line\n[00:05.50] Three line";

// ── precedence ──
{
  const entry = { synced: OWN, offsetSec: 0.5 };
  const own = timingOf(entry);
  check("the lyrics' own timing, at the offset, when nothing else is kept", own.kind === "offset" && same(times(own.lines), [0.5, 1.5, 3.5, 5.5]) && own.offset === 0.5);
  const aligned = timingOf({ ...entry, aligned: { synced: ALIGNED } });
  check("the server's timing from the vocals over the offset", aligned.kind === "aligned" && same(times(aligned.lines), [0, 1.2, 3.1, 5.4]));
  const user = timingOf({ ...entry, aligned: { synced: ALIGNED }, userSynced: USER });
  check("the user's own timing over both", user.kind === "user" && same(times(user.lines), [0, 1.5, 3.5, 5.5]));
  const broken = timingOf({ ...entry, userSynced: "not lrc at all" });
  check("user timing with no timed lines falls through", broken.kind === "offset");
  const plain = timingOf({ plain: "Just words" });
  check("plain lyrics have no timing at all", plain.lines.length === 0);
  const plainAligned = timingOf({ plain: "One line", aligned: { synced: "[00:02.00] One line" } });
  check("plain lyrics the server fitted to the vocals are timed", plainAligned.kind === "aligned" && plainAligned.lines.length === 1);
}

// ── lyrics.json's new fields ──
{
  const found = fromServerLyrics({
    source: "lrclib",
    track: "S",
    synced: OWN,
    offset_sec: 1,
    user_synced: USER,
    aligned: { synced: ALIGNED, method: "dtw", matched: 0.9, lines_matched: 3, lines: 4, at: 1700000000 },
  });
  check("user_synced is carried as userSynced", found.entry.userSynced === USER);
  check("aligned is carried with its counts", found.entry.aligned?.synced === ALIGNED && found.entry.aligned.linesMatched === 3 && found.entry.aligned.lines === 4 && found.entry.aligned.method === "dtw");
  const bare = fromServerLyrics({ source: "lrclib", track: "S", synced: OWN, user_synced: "", aligned: { synced: "" } });
  check("empty timing fields are left out", !("userSynced" in bare.entry) && !("aligned" in bare.entry));
}

// ── LRC out ──
{
  check("stamps are mm:ss.xx", formatStamp(65.237) === "01:05.24" && formatStamp(0) === "00:00.00" && formatStamp(-2) === "00:00.00");
  check("an hour and more still reads back", parseLrc(`[${formatStamp(3725.5)}] Late`)[0].time === 3725.5);
  const lines = parseLrc(ALIGNED);
  const lrc = serializeLrc(lines);
  const back = parseLrc(lrc);
  check("LRC written reads back to the same lines", same(back.map((l) => [l.time, l.text]), lines.map((l) => [l.time, l.text])), lrc);
  check("word stamps survive the round trip", same(back[1].words?.map((w) => w.time), [1.2, 1.6]) && back[1].words.map((w) => w.text).join("") === "One line");
  check("an empty line stays a bare stamp", lrc.split("\n")[0] === "[00:00.00]");
  check("one line per stamp", lrc.split("\n").length === lines.length);
}

// ── words ──
{
  const line = { time: 10, text: "a b c", words: [{ time: 10, text: "a " }, { time: 11, text: "b " }, { time: 12, text: "c" }] };
  check("words with room are left alone", fitWords(line, 20) === line);
  const squeezed = fitWords(line, 11.15);
  check("words squeezed in proportion to fit before the next line", same(squeezed.words.map((w) => Math.round(w.time * 1000) / 1000), [10, 10.5, 11]));
  check("a line without word stamps is left alone", fitWords({ time: 1, text: "x" }, 1.1).words === undefined);

  const lines = parseLrc("[00:01.00]<00:01.00>One <00:01.50>two\n[00:04.00]<00:04.00>Three <00:04.50>four\n[00:08.00] End");
  const moved = applyTime(lines, 1, 5);
  check("a moved line's words move with it", same(moved[1].words.map((w) => w.time), [5, 5.5]));
  const early = applyTime(lines, 1, 1.4);
  check("the line before one moved closer has its words squeezed", early[0].words.every((w) => w.time < 1.4) && near(early[0].words[1].time, 1 + 0.5 * (0.25 / 0.5)));
  check("the lines given are left as they were", lines[1].time === 4 && lines[1].words[0].time === 4);
}

// ── moving one line ──
{
  const lines = parseLrc("[00:01.00] A\n[00:03.00] B\n[00:05.00] C");
  check("clamp: a line moves between its neighbours", same(times(applyTime(lines, 1, 4)), [1, 4, 5]));
  check("clamp: never past the next line", same(times(applyTime(lines, 1, 9)), [1, 5 - MIN_GAP_SEC, 5]));
  check("clamp: never before the line above", same(times(applyTime(lines, 1, 0)), [1, 1 + MIN_GAP_SEC, 5]));
  check("never before the track starts", applyTime(lines, 0, -3)[0].time === 0);
  check("push: the lines passed are pushed ahead", same(times(applyTime(lines, 0, 4, { mode: "push" })), [4, 4.1, 5]));
  check("push: earlier lines are pulled back", same(times(applyTime(lines, 2, 0.5, { mode: "push" })), [0.3, 0.4, 0.5]));
  check("ripple: the lines after move by as much", same(times(applyTime(lines, 0, 2, { mode: "ripple" })), [2, 4, 6]));
  check("ripple stops at a line already set", same(times(applyTime(lines, 0, 2, { mode: "ripple", fixed: new Set([2]) })), [2, 4, 5]));
  check("ripple then pushes what it would cross", same(times(applyTime(lines, 0, 4.5, { mode: "ripple", fixed: new Set([2]) })), [4.5, 6.5, 6.6]));
  check("the track's length caps a line", applyTime(lines, 2, 99, { max: 6 })[2].time === 6);
  const ordered = (ls) => ls.every((l, i) => i === 0 || l.time >= ls[i - 1].time);
  let fuzz = true;
  for (let k = 0; k < 200; k++) {
    const i = k % 3;
    const t = (k * 7.31) % 9 - 1;
    for (const mode of ["clamp", "push", "ripple"]) fuzz &&= ordered(applyTime(lines, i, t, { mode }));
  }
  check("no move ever puts lines out of order", fuzz);
}

// ── tapping, and undo ──
{
  const lines = parseLrc("[00:00.00]\n[00:01.00] A\n[00:03.00] B\n[00:05.00] C");
  const session = new SyncSession(lines, { ripple: true, max: 6 });
  check("the first sung line is in hand to begin with", session.cursor === 1);
  session.tap(1.4);
  check("a tap sets the line in hand and carries the rest along", same(times(session.lines), [0, 1.4, 3.4, 5.4]) && session.cursor === 2);
  session.tap(3.2);
  check("the next tap sets the next line", same(times(session.lines), [0, 1.4, 3.2, 5.2]) && session.cursor === 3);
  session.undo();
  check("undo takes the last tap back, and the line is in hand again", same(times(session.lines), [0, 1.4, 3.4, 5.4]) && session.cursor === 2);
  session.redo();
  check("redo puts it back", same(times(session.lines), [0, 1.4, 3.2, 5.2]) && session.cursor === 3);
  session.tap(5.9);
  check("after the last line every line is set", session.done && session.tap(6) === false);
  session.undo();
  session.undo();
  session.undo();
  check("undo all the way goes back to the start", same(times(session.lines), [0, 1, 3, 5]) && session.cursor === 1 && !session.canUndo);

  const from = new SyncSession(lines, { ripple: true });
  from.select(3);
  from.tap(5.5);
  check("tapping can start from any line", same(times(from.lines), [0, 1, 3, 5.5]) && from.done);
  from.select(1);
  from.tap(2);
  check("a tap does not carry lines already set", same(times(from.lines), [0, 2, 4, 5.5]));

  const still = new SyncSession(lines, { ripple: false });
  still.tap(2);
  check("without ripple a tap moves only its line, if nothing is crossed", same(times(still.lines), [0, 2, 3, 5]));
  still.tap(4);
  check("and pushes a line it would cross", same(times(still.lines), [0, 2, 4, 5]) && still.cursor === 3);
  const twice = new SyncSession(lines, { ripple: false });
  twice.tap(1);
  check("a tap on the time a line already has still moves on, and undoes", twice.cursor === 2 && twice.undo() && twice.cursor === 1);

  const meta = new SyncSession(lines, { meta: { kind: "aligned" } });
  meta.move(1, 1.5);
  meta.meta.kind = "user";
  meta.undo();
  check("undo brings back the caller's note of which timing it was", meta.meta.kind === "aligned");
  check("a nudge moves the line in hand", meta.nudge(1, 0.05) && near(meta.lines[1].time, 1.05));
  meta.replace(parseLrc(OWN));
  check("a reset is one step to undo", meta.undo() && near(meta.lines[1].time, 1.05));
  check("next sung line skips empty stamps", nextSungIndex(lines, -1) === 1 && nextSungIndex(lines, 3) === 4);
}

// ── snapping to the singing ──
{
  // 40ms frames: silence, then singing from 2.0 s to 3.0 s.
  const hop = 0.04;
  const db = new Int8Array(150).fill(-80);
  for (let i = 50; i < 75; i++) db[i] = -12;
  const envelope = { hop, db };
  check("a tap just after the singing starts snaps back to it", near(snapToVoice(envelope, 2.2), 2.0, 1e-9));
  check("a tap just before snaps forward", near(snapToVoice(envelope, 1.85), 2.0, 1e-9));
  check("too far away, the tap stays", snapToVoice(envelope, 1.2) === 1.2);
  check("no envelope, no snap", snapToVoice(null, 2.2) === 2.2);
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
