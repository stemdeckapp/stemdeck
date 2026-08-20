// Regression test for #401: a failure notification should auto-clear once
// whatever it was about is resolved (job superseded/removed, retried
// successfully), without touching the #359 behavior of surviving a plain
// reload. Covers dismissFailuresByJobId and dismissFailuresByKind directly.
//
// Run:  node tests/js/notifications-resolve.test.mjs

// notifyFailure/dismissFailure* touch window/document (persist -> storeSet,
// render -> DOM); stub both before import, matching wav-header.test.mjs's
// precedent. storeGet/storeSet fall through to a caught localStorage access
// under this stub, so nothing throws.
globalThis.window = {};
globalThis.document = { getElementById: () => null, querySelectorAll: () => [] };

const {
  notifyFailure,
  dismissFailuresByJobId,
  dismissFailuresByKind,
  clearFailures,
  getFailures,
} = await import("../../static/js/notifications.js");

let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};

const kinds = () => getFailures().map((f) => f.kind).sort();

// --------------------------------------------------------------------------
// dismissFailuresByJobId
// --------------------------------------------------------------------------

clearFailures();
notifyFailure({ kind: "import", message: "import broke", context: { jobId: "job-1" } });
notifyFailure({ kind: "playback", message: "playback broke", context: { jobId: "job-1" } });
notifyFailure({ kind: "import", message: "other track broke", context: { jobId: "job-2" } });

dismissFailuresByJobId("job-1", "playback");
check(
  "by jobId+kind clears only the matching kind for that id",
  JSON.stringify(kinds()) === JSON.stringify(["import", "import"]),
  JSON.stringify(kinds()),
);

dismissFailuresByJobId("job-1");
check(
  "by jobId (no kind) clears every kind for that id",
  JSON.stringify(kinds()) === JSON.stringify(["import"]),
  JSON.stringify(kinds()),
);
check("unrelated job's failure survives", getFailures().some((f) => f.context?.jobId === "job-2"));

clearFailures();
dismissFailuresByJobId(null);
check("dismissFailuresByJobId(null) is a no-op, does not throw", getFailures().length === 0);

// --------------------------------------------------------------------------
// dismissFailuresByKind
// --------------------------------------------------------------------------

clearFailures();
notifyFailure({ kind: "update", message: "check failed" });
notifyFailure({ kind: "export", message: "log export failed" }); // no jobId
notifyFailure({ kind: "export", message: "track export failed", context: { jobId: "job-3" } });

dismissFailuresByKind("export");
check(
  "by kind never touches a record that carries a jobId",
  getFailures().some((f) => f.kind === "export" && f.context?.jobId === "job-3"),
);
check(
  "by kind clears the id-less record of that kind",
  !getFailures().some((f) => f.kind === "export" && !f.context?.jobId),
);
check("unrelated kind survives", getFailures().some((f) => f.kind === "update"));

// --------------------------------------------------------------------------
// No-op guard: nothing to clear leaves the list untouched
// --------------------------------------------------------------------------

clearFailures();
notifyFailure({ kind: "import", message: "still broken", context: { jobId: "job-4" } });
dismissFailuresByJobId("job-does-not-exist");
dismissFailuresByKind("playback");
check("no-op dismisses leave unrelated failures in place", getFailures().length === 1);

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
