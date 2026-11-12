// Pure logic behind take folders (static/js/capture/takeFolderModel.js): which
// takes a track's folder lists and in what order, when the folder is open,
// how the stored choice is read and kept bounded, and where the take lanes sit
// in the lane stack.
//
// Run:  node tests/js/take-folders.test.mjs

import {
  takesNewestFirst, parseFolderStore, isFolderOpen, withFolderChoice, laneOrder, takeLaneOrder,
  openFolders, folderRowCount, nudgeText, FOLDER_STORE_MAX, LANE_ORDER_STEP,
} from "../../static/js/capture/takeFolderModel.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ─── Order ───
{
  const takes = [
    { id: "t00000001", created_at: 100 },
    { id: "t00000002", created_at: 300 },
    { id: "t00000003", created_at: 200 },
  ];
  const ids = takesNewestFirst(takes).map((t) => t.id);
  check("newest take first, by created_at", same(ids, ["t00000002", "t00000003", "t00000001"]), ids.join(","));
  check("the input list is not reordered", takes[0].id === "t00000001");
  const tied = takesNewestFirst([{ id: "ta", created_at: 5 }, { id: "tb", created_at: 5 }]).map((t) => t.id);
  check("equal times: the later one in the list is newer", same(tied, ["tb", "ta"]), tied.join(","));
  const legacy = takesNewestFirst([{ id: "ta" }, { id: "tb" }]).map((t) => t.id);
  check("no created_at (older takes): list order, reversed", same(legacy, ["tb", "ta"]));
  check("not a list: nothing", takesNewestFirst(null).length === 0);
  check("entries without an id are dropped", takesNewestFirst([null, {}, { id: "tc" }]).length === 1);
}

// ─── Open or closed ───
{
  const open = (takeCount, store = {}, session = {}) => isFolderOpen({ trackId: "k1", takeCount, store, session });
  check("no takes: closed", open(0) === false);
  check("no takes: closed even if stored open", open(0, { k1: true }) === false);
  check("one take: closed by default", open(1) === false);
  check("one take: a stored choice does not open it", open(1, { k1: true }) === false);
  check("one take: open when opened in this page", open(1, {}, { k1: true }) === true);
  check("two takes: open by default", open(2) === true);
  check("two takes: the stored choice wins (closed)", open(2, { k1: false }) === false);
  check("two takes: the stored choice wins (open)", open(5, { k1: true }) === true);
  check("another track's choice does not apply", open(3, { k2: false }) === true);
}

// ─── Stored choices ───
{
  check("empty or missing: nothing stored", same(parseFolderStore(null), {}) && same(parseFolderStore(""), {}));
  check("bad JSON: nothing stored", same(parseFolderStore("{oops"), {}));
  check("an array: nothing stored", same(parseFolderStore("[true]"), {}));
  check("non-boolean values are dropped", same(parseFolderStore('{"k1":true,"k2":"yes","k3":false}'), { k1: true, k3: false }));

  const s1 = withFolderChoice({ k1: true }, "k2", false);
  check("a choice is added", same(s1, { k1: true, k2: false }));
  const s2 = withFolderChoice(s1, "k1", false);
  check("a changed choice moves to the newest place", same(Object.keys(s2), ["k2", "k1"]) && s2.k1 === false);
  check("the old store is not changed", s1.k1 === true);

  let big = {};
  for (let i = 0; i < FOLDER_STORE_MAX + 25; i++) big = withFolderChoice(big, `k${i}`, i % 2 === 0);
  const keys = Object.keys(big);
  check("the store is bounded", keys.length === FOLDER_STORE_MAX, String(keys.length));
  check("the oldest choices go first", !("k0" in big) && `k${FOLDER_STORE_MAX + 24}` in big);
}

// ─── Place in the stack ───
{
  check("lanes step by LANE_ORDER_STEP", laneOrder(0) === 0 && laneOrder(3) === 3 * LANE_ORDER_STEP);
  check("a take lane sits right after its track and its EQ row", takeLaneOrder(4, 0) === laneOrder(4) + 2);
  check("take lanes keep their order", takeLaneOrder(4, 0) < takeLaneOrder(4, 1) && takeLaneOrder(4, 1) < takeLaneOrder(4, 2));
  check("the last take lane is still before the next track", takeLaneOrder(4, 49) < laneOrder(5));
  check("even past the cap, it never reaches the next track", takeLaneOrder(4, 500) < laneOrder(5));
}

// ─── Open folders and row count ───
{
  const tA = { id: "ta1", created_at: 1 };
  const tB = { id: "ta2", created_at: 2 };
  const tracks = [
    { id: "kA", takes: [tA, tB], active: tA },
    { id: "kB", takes: [], active: null },
    { id: "kC", takes: [{ id: "tc1", created_at: 1 }], active: { id: "tc1" } },
  ];
  const all = openFolders(tracks, () => true);
  check("tracks with no takes have no folder", same(all.map((f) => f.trackId), ["kA", "kC"]));
  check("a folder lists its takes newest first", same(all[0].takes.map((x) => x.take.id), ["ta2", "ta1"]));
  check("the active take is the one marked playing", same(all[0].takes.map((x) => x.playing), [false, true]));
  check("rows: one per take of every open folder", folderRowCount(all) === 3);
  const onlyA = openFolders(tracks, (tr) => tr.id === "kA");
  check("closed folders add no rows", folderRowCount(onlyA) === 2);
  check("nothing open: no rows", folderRowCount(openFolders(tracks, () => false)) === 0);
}

// ─── Nudge text ───
{
  check("no nudge: no text", nudgeText(0) === "" && nudgeText(undefined) === "");
  check("positive: plus sign", nudgeText(12) === "+12 ms");
  check("negative: minus sign", nudgeText(-3.4) === "−3 ms", nudgeText(-3.4));
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
