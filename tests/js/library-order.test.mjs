// The Library's order (#772): oldest first by when each song was added, the
// order the Queue runs them in, with the id as a fixed tie-break.
//
// Run:  node tests/js/library-order.test.mjs

import { compareOldestFirst } from "../../static/js/shared/jobs.js";

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

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
