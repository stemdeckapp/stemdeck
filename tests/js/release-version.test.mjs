// Version ordering behind the update card (static/js/releaseVersion.js).
//
// The card used to show whenever the latest release differed from the running
// version, so a release candidate ahead of the public release (0.21.0-rc.1
// while v0.20.0 is latest) was offered "update to v0.20.0", a downgrade.
// Nothing about that is visible on a developer machine, because dev builds
// never see the card at all. These checks pin the ordering in both spellings
// a version arrives in: the GitHub tag (0.21.0-rc.1) and the PEP 440 package
// version from /api/health (0.21.0rc1).
//
// Run:  node tests/js/release-version.test.mjs

import {
  compareVersions,
  isDevVersion,
  isNewerRelease,
  parseVersion,
} from "../../static/js/releaseVersion.js";

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

const cmp = (a, b, expected) => {
  const got = compareVersions(a, b);
  check(`compare ${a} vs ${b} -> ${expected}`, got === expected, String(got));
  // Ordering has to be antisymmetric, or a sort and the card disagree.
  const back = compareVersions(b, a);
  check(`compare ${b} vs ${a} -> ${-expected || 0}`, back === (-expected || 0), String(back));
};

// ─── equal: two spellings of one version ───

for (const [a, b] of [
  ["0.20.0", "0.20.0"],
  ["v0.20.0", "0.20.0"],
  ["V0.20.0", "0.20.0"],
  [" 0.20.0 ", "0.20.0"],
  ["0.20", "0.20.0"],
  ["0.20.0.0", "0.20"],
  ["0.21.0-rc.1", "0.21.0rc1"],
  ["0.21.0-RC.1", "0.21.0rc1"],
  ["0.21.0rc", "0.21.0rc0"],
  ["0.21.0c1", "0.21.0rc1"],
  ["0.21.0pre1", "0.21.0rc1"],
  ["0.21.0-preview.1", "0.21.0rc1"],
  ["0.7.0-alpha.9", "0.7.0a9"],
  ["0.7.0_alpha_9", "0.7.0a9"],
  ["0.7.0-beta.2", "0.7.0b2"],
  ["1.0-1", "1.0.post1"],
  ["1.0.rev1", "1.0.post1"],
  ["1.0-dev2", "1.0.dev2"],
  ["0!1.0", "1.0"],
  ["1.0+ABC", "1.0+abc"],
  ["1.0+a-b_c", "1.0+a.b.c"],
]) {
  cmp(a, b, 0);
}

// ─── older and newer finals ───

cmp("0.19.0", "0.20.0", -1);
cmp("0.20.0", "0.20.1", -1);
cmp("0.9.0", "0.10.0", -1); // numeric, not text: "0.9" sorts after "0.10" as text
cmp("0.20.10", "0.20.9", 1);
cmp("1.0.0", "0.99.99", 1);
cmp("0.20", "0.20.1", -1);
cmp("1!0.1", "2.0", 1); // epoch wins over release

// ─── rc against final, in both spellings ───

cmp("0.20.0", "0.21.0rc1", -1);
cmp("0.20.0", "0.21.0-rc.1", -1);
cmp("0.21.0rc1", "0.21.0", -1);
cmp("0.21.0-rc.1", "v0.21.0", -1);
cmp("0.20.9", "0.21.0-rc.1", -1);

// ─── rc against rc ───

cmp("0.21.0rc1", "0.21.0rc2", -1);
cmp("0.21.0-rc.1", "0.21.0rc2", -1);
cmp("0.21.0rc9", "0.21.0rc10", -1);
cmp("0.21.0rc2", "0.22.0rc1", -1);

// ─── alpha < beta < rc < final ───

const ladder = ["0.7.0.dev0", "0.7.0a1", "0.7.0-alpha.9", "0.7.0b1", "0.7.0-beta.2", "0.7.0rc1", "0.7.0", "0.7.0+local", "0.7.0.post1"];
for (let i = 0; i < ladder.length; i++) {
  for (let j = i + 1; j < ladder.length; j++) cmp(ladder[i], ladder[j], -1);
}

// ─── dev builds ───

cmp("0.18.2.dev0", "0.18.2", -1); // dev of a final precedes it
cmp("0.18.2.dev0", "0.18.1", 1); // and follows the release before
cmp("0.18.2.dev0", "0.18.2a1", -1); // a bare dev precedes every pre-release
cmp("0.7.0a5.dev3", "0.7.0a5", -1); // dev of a pre-release precedes that pre
cmp("0.7.0a5.dev3", "0.7.0a4", 1);
cmp("0.7.0a5.dev3", "0.7.0a5.dev4", -1);
cmp("1.0.post1.dev0", "1.0.post1", -1);
cmp("1.0.post1.dev0", "1.0", 1);
cmp("0.21.0.dev3+g1a2b3c4", "0.20.0", 1);

// ─── local versions ───

cmp("0.7.0a5.dev3+g1a2b3c4", "0.7.0a5.dev3", 1);
cmp("1.0+1", "1.0+abc", 1); // numeric segment beats alphanumeric
cmp("1.0+2", "1.0+10", -1); // numbers compare as numbers
cmp("1.0+abc", "1.0+abd", -1);
cmp("1.0+abc", "1.0+abc.1", -1); // a prefix sorts first
cmp("0.18.2+local", "0.18.2", 1);

// ─── garbage ───

for (const bad of ["", "   ", null, undefined, "latest", "v", "1.0.x", "1..0", "1.0-", "abc1.0", "1.0 beta", "1.0+", "1.0+a..b", "0.21.0-rc.1-extra", "🙂"]) {
  check(`unparseable: ${JSON.stringify(bad)}`, parseVersion(bad) === null);
  check(`compare refuses to guess: ${JSON.stringify(bad)}`, compareVersions(bad, "1.0") === null && compareVersions("1.0", bad) === null);
}

// ─── the decision the card makes ───

const offer = (latest, current, expected, why) => {
  const got = isNewerRelease(latest, current);
  check(`offer ${latest} to ${current}: ${expected} (${why})`, got === expected, String(got));
};

offer("v0.20.0", "0.21.0-rc.1", false, "the reported bug: a downgrade");
offer("v0.20.0", "0.21.0rc1", false, "same, in the package spelling");
offer("v0.20.0", "0.20.1", false, "running build is newer");
offer("v0.20.0", "0.20.0", false, "already current");
offer("v0.7.0-alpha.9", "0.7.0a9", false, "already current, two spellings");
offer("v0.20.0", "0.19.3", true, "older final");
offer("v0.21.0", "0.21.0rc1", true, "rc to its final");
offer("v0.21.0", "0.21.0-rc.1", true, "rc to its final, tag spelling");
offer("v0.7.0-beta.1", "0.7.0a9", true, "alpha to beta");
offer("v0.20.0", "0.1.0", true, "health check failed, fallback version");
// An unreadable latest tag is never offered: this code cannot tell whether
// it is a downgrade, and a wrong card reaches every install.
offer("nightly", "0.19.0", false, "unparseable latest");
offer("", "0.19.0", false, "empty latest");
// An unreadable running version keeps the old "differs" test, so a build
// with a packaging fault still hears about a release.
offer("v0.20.0", "unknown-build", true, "unparseable current, differs");
offer("v0.20.0", "0.20.0 (custom)", true, "unparseable current, differs");

// ─── isDevVersion, unchanged behaviour ───

for (const [v, expected] of [
  ["0.18.2.dev0", true],
  ["0.7.0a5.dev3+g1a2b3c4", true],
  ["0.18.2+local", true],
  ["0.18.2dev", true],
  ["0.21.0rc1", false],
  ["0.21.0-rc.1", false],
  ["v0.20.0", false],
]) {
  check(`isDevVersion(${v}) -> ${expected}`, isDevVersion(v) === expected);
}

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
