// Performance Mode's take lanes (static/js/streamer/takeListModel.js).
import {
  TAKE_LANES_MIN, activeTakeId, takeLaneRows, showsTakeLanes, chooseAction, followTake, scrollTarget, takeLanesSig,
} from "../../static/js/streamer/takeListModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const t1 = { id: "t1", label: "Take 1", created_at: 100 };
const t2 = { id: "t2", label: "Take 2", created_at: 200 };
const t3 = { id: "t3", label: "", created_at: 300 };
// The server's order is not the rows' order: the rows sort by time.
const track = { id: "k1", takes: [t3, t1, t2], active: t1, active_take_id: "t1" };

// Which take plays
check("active: the track's active take", activeTakeId(track) === "t1");
check("active: the server's field when the take is not joined", activeTakeId({ active_take_id: "t2" }) === "t2");
check("active: take_id spelling", activeTakeId({ active: { take_id: "t3" } }) === "t3");
check("active: none", activeTakeId({}) === null && activeTakeId(null) === null && activeTakeId({ active_take_id: "" }) === null);

// The rows: oldest on top, newest at the bottom, the one playing marked
const rows = takeLaneRows(track);
check("rows: oldest first", eq(rows.map((r) => r.id), ["t1", "t2", "t3"]), JSON.stringify(rows));
check("rows: numbered from 1 in that order", eq(rows.map((r) => r.n), [1, 2, 3]));
check("rows: exactly one marked, the active take", eq(rows.map((r) => r.active), [true, false, false]));
check("rows: a take without a label is named by its id", rows[2].label === "t3" && rows[1].label === "Take 2");
check("rows: equal times keep the server's order", eq(takeLaneRows({ takes: [{ id: "a", created_at: 1 }, { id: "b", created_at: 1 }] }).map((r) => r.id), ["a", "b"]));
check("rows: none for no track or no takes", eq(takeLaneRows(null), []) && eq(takeLaneRows({ takes: null }), []));
check("rows: an active id that is not a take marks none", takeLaneRows({ takes: [t1, t2], active_take_id: "gone" }).every((r) => !r.active));

// When the rows show
check("shows from two takes", TAKE_LANES_MIN === 2 && showsTakeLanes(takeLaneRows({ takes: [t1, t2] })));
check("hidden with one take or none", !showsTakeLanes(takeLaneRows({ takes: [t1] })) && !showsTakeLanes([]) && !showsTakeLanes(null));

// What a click does
check("choose: another take is selected", chooseAction(rows, "t2", { laneTake: "t1" }) === "select");
check("choose: the playing take shown in the lane does nothing", chooseAction(rows, "t1", { laneTake: "t1" }) === "none");
check("choose: the playing take, lane showing another: the lane follows", chooseAction(rows, "t1", { laneTake: "t3" }) === "keep" && chooseAction(rows, "t1") === "keep");
check("choose: nothing while locked (recording or exporting)", chooseAction(rows, "t2", { locked: true }) === "none" && chooseAction(rows, "t1", { locked: true, laneTake: "t3" }) === "none");
check("choose: an unknown take does nothing", chooseAction(rows, "zz") === "none" && chooseAction(rows, null) === "none" && chooseAction(null, "t2") === "none");

// The take lane follows the take that plays
check("follow: the lane shows another row: it follows the playing take", followTake(rows, "t3") === "t1");
check("follow: the lane shows nothing: it follows", followTake(rows, null) === "t1");
check("follow: already the playing take: nothing", followTake(rows, "t1") === null);
check("follow: a take just recorded, not a row yet: wait", followTake(rows, "t9") === null);
check("follow: never while locked, or with one take", followTake(rows, "t3", { locked: true }) === null && followTake(takeLaneRows({ takes: [t1], active: t1 }), null) === null);
check("follow: no take plays: nothing", followTake(takeLaneRows({ takes: [t1, t2] }), "t1") === null);

// What scrolls into view
check("scroll: shown anew: the playing row", scrollTarget(null, rows) === "t1");
check("scroll: a take recorded since: the newest row", scrollTarget(["t1", "t2"], rows) === "t3");
check("scroll: nothing added: nothing", scrollTarget(["t1", "t2", "t3"], rows) === null && scrollTarget(["t1", "t2", "t3", "t4"], rows) === null);
check("scroll: rows hidden: nothing", scrollTarget(null, takeLaneRows({ takes: [t1], active: t1 })) === null);

// The rebuild signature
check("sig: changes with the take that plays", takeLanesSig(rows, false) !== takeLanesSig(takeLaneRows({ ...track, active: t2, active_take_id: "t2" }), false));
check("sig: changes with the lock and a label", takeLanesSig(rows, false) !== takeLanesSig(rows, true)
  && takeLanesSig(rows, false) !== takeLanesSig(takeLaneRows({ ...track, takes: [t1, { ...t2, label: "Best" }, t3] }), false));
check("sig: the same rows, the same string", takeLanesSig(takeLaneRows(track), false) === takeLanesSig(rows, false));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
