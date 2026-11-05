// Transposed export on an FFmpeg without rubberband (#858): when an export
// that carries a key change is held back (static/js/transposeExport.js).
//
// Run:  node tests/js/transpose-export.test.mjs

import {
  transposeExportFromHealth,
  isTransposed,
  transposeExportBlocked,
} from "../../static/js/transposeExport.js";

let passed = 0;
let failed = 0;

function check(name, condition, detail = "") {
  if (condition) {
    passed++;
    console.log(`PASS  ${name}`);
  } else {
    failed++;
    console.log(`FAIL  ${name}${detail ? `  -- ${detail}` : ""}`);
  }
}

// The health answer: only a real boolean counts.
check("health true is true", transposeExportFromHealth({ transpose_export: true }) === true);
check("health false is false", transposeExportFromHealth({ transpose_export: false }) === false);
check("health null is unknown", transposeExportFromHealth({ transpose_export: null }) === null);
check("an older backend without the field is unknown", transposeExportFromHealth({ status: "ok" }) === null);
check("no health at all is unknown", transposeExportFromHealth(null) === null);
check("a string is not an answer", transposeExportFromHealth({ transpose_export: "false" }) === null);

// A key change, as the export would send it.
check("every key at 0 is not transposed", !isTransposed([0, 0, 0]));
check("an empty mix is not transposed", !isTransposed([]));
check("no keys at all is not transposed", !isTransposed(undefined));
check("one lane up is transposed", isTransposed([0, 2, 0]));
check("one lane down is transposed", isTransposed([-1]));
check("a missing key reads as 0", !isTransposed([0, undefined, null]));

// The decision: held back only on a known "no" with a key change.
check("no rubberband and transposed: held back", transposeExportBlocked([0, 3], false));
check("no rubberband and every key at 0: offered", !transposeExportBlocked([0, 0], false));
check("rubberband and transposed: offered", !transposeExportBlocked([0, 3], true));
check("unknown and transposed: offered (the backend still says why)", !transposeExportBlocked([0, 3], null));
check("no rubberband and no lanes: offered", !transposeExportBlocked([], false));

console.log(`\n${passed}/${passed + failed} transpose export checks passed`);
process.exit(failed ? 1 : 0);
