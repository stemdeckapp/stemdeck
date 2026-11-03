// The Library's order (#772): oldest first by when each song was added, the
// order the Queue runs them in, with the id as a fixed tie-break.
//
// Run:  node tests/js/library-order.test.mjs

import { addedAt, compareOldestFirst } from "../../static/js/shared/jobs.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const ids = (list) => list.map((x) => x.id).join(",");

const added = [
  { id: "c", createdAt: 300 },
  { id: "a", createdAt: 100 },
  { id: "b", createdAt: 200 },
];
check("the first song added comes first", ids([...added].sort(compareOldestFirst)) === "a,b,c");

const sameSecond = [
  { id: "y", createdAt: 100 },
  { id: "x", createdAt: 100 },
];
check("songs added in the same second follow their id", ids([...sameSecond].sort(compareOldestFirst)) === "x,y");
check(
  "the tie-break does not depend on the stored order",
  ids([...sameSecond].reverse().sort(compareOldestFirst)) === "x,y",
);

const noTime = [
  { id: "n", createdAt: 50 },
  { id: "m" },
];
check("a song with no time is treated as the oldest", ids([...noTime].sort(compareOldestFirst)) === "m,n");

// A browser whose clock runs an hour behind the server's (#978). Song A was
// added first; song B is added in this session ten minutes later, by the
// server's clock, and its row is stamped by the browser before the server
// has sent B's state. The ids sort the other way, so only the time can put
// A first.
const SKEW = -3600;
const serverA = 1_800_000_000;
const serverB = serverA + 600;
const rowA = { id: "y", createdAt: addedAt(serverA, undefined) };
const rowB = { id: "x", createdAt: addedAt(undefined, undefined, serverB + SKEW) };
check("a new row is stamped by this browser until the server has sent its time", rowB.createdAt === serverB + SKEW);
rowB.createdAt = addedAt(serverB, rowB.createdAt);
check("the server's time replaces the browser's stamp once it arrives", rowB.createdAt === serverB, `got ${rowB.createdAt}`);
check(
  "with a skewed browser clock the Library still lists the song added first first",
  ids([rowB, rowA].sort(compareOldestFirst)) === "y,x",
);
check("a row the server has not described keeps the time it has", addedAt(undefined, 1234, 9999) === 1234);

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
