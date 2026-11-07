// Whether the latest public release is an update for the running build.
//
// The update card used to appear whenever the latest release differed from
// the running version. A build ahead of the latest release, such as
// 0.21.0-rc.1 while v0.20.0 is latest, was then offered "update to v0.20.0",
// which is a downgrade. The card is only right when the release is strictly
// newer, so this orders versions instead of testing them for equality.
//
// Two spellings of one version reach this code: the GitHub release tag
// (0.7.0-alpha.9, 0.21.0-rc.1) and the backend's PEP 440 package version from
// hatch-vcs via /api/health (0.7.0a9, 0.21.0rc1, 0.18.2.dev0,
// 0.7.0a5.dev3+g1a2b3c4). Both are read with PEP 440's own normalisation
// rules, so a tag and a package version of the same release compare equal.
//
// Ordering follows PEP 440: 1.0.dev0 < 1.0a1 < 1.0b1 < 1.0rc1 < 1.0 <
// 1.0+local < 1.0.post1. Pure, so the node tests can import it.

// PEP 440's normalisation-tolerant form, the same shape the `packaging`
// library accepts: epoch, release, pre, post, dev, local.
const PEP440 = new RegExp(
  "^v?" +
    "(?:(\\d+)!)?" + // epoch
    "(\\d+(?:\\.\\d+)*)" + // release
    "(?:[-_.]?(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\\d+)?)?" + // pre
    "(?:-(\\d+)|[-_.]?(post|rev|r)[-_.]?(\\d+)?)?" + // post
    "(?:[-_.]?(dev)[-_.]?(\\d+)?)?" + // dev
    "(?:\\+([a-z0-9]+(?:[-_.][a-z0-9]+)*))?" + // local
    "$",
  "i",
);

// alpha, beta, rc in that order. "c", "pre" and "preview" are PEP 440
// spellings of rc.
const PRE_RANK = { a: 0, alpha: 0, b: 1, beta: 1, c: 2, rc: 2, pre: 2, preview: 2 };

/**
 * Read a version string into its PEP 440 parts, or null when it is not one.
 * A leading "v" (the tag form) and surrounding space are allowed.
 */
export function parseVersion(value) {
  const m = PEP440.exec(String(value ?? "").trim());
  if (!m) return null;
  const [, epoch, release, preL, preN, postImplicit, postL, postN, devL, devN, local] = m;
  const parts = release.split(".").map(Number);
  // 1.0 and 1.0.0 are the same release.
  while (parts.length > 1 && parts[parts.length - 1] === 0) parts.pop();
  let post = null;
  if (postImplicit !== undefined) post = Number(postImplicit);
  else if (postL !== undefined) post = Number(postN ?? 0);
  return {
    epoch: Number(epoch ?? 0),
    release: parts,
    pre: preL !== undefined ? [PRE_RANK[preL.toLowerCase()], Number(preN ?? 0)] : null,
    post,
    dev: devL !== undefined ? Number(devN ?? 0) : null,
    local: local !== undefined ? local.toLowerCase().split(/[-_.]/) : null,
  };
}

const sign = (n) => (n < 0 ? -1 : n > 0 ? 1 : 0);

function compareNumberLists(a, b) {
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d) return sign(d);
  }
  return 0;
}

// The pre-release slot as a sortable pair. A version with no pre-release is
// the final release and sorts after every pre-release of it, except that a
// bare dev build of the final (1.0.dev0) sorts before all of them.
function preKey(v) {
  if (v.pre) return v.pre;
  if (v.post === null && v.dev !== null) return [-Infinity, 0];
  return [Infinity, 0];
}

// PEP 440 local segments: numeric beats alphanumeric, numbers compare as
// numbers, words as text, and a shorter run that is a prefix sorts first.
function compareLocal(a, b) {
  if (a === null || b === null) return a === b ? 0 : a === null ? -1 : 1;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    if (a[i] === undefined) return -1;
    if (b[i] === undefined) return 1;
    const an = /^\d+$/.test(a[i]);
    const bn = /^\d+$/.test(b[i]);
    if (an && bn) {
      const d = Number(a[i]) - Number(b[i]);
      if (d) return sign(d);
    } else if (an !== bn) {
      return an ? 1 : -1;
    } else if (a[i] !== b[i]) {
      return a[i] < b[i] ? -1 : 1;
    }
  }
  return 0;
}

/**
 * Order two versions: -1 when a is older, 0 when equal, 1 when a is newer.
 * Returns null when either side cannot be read as a version, so a caller
 * has to decide what an unreadable version means instead of getting a guess.
 */
export function compareVersions(a, b) {
  const va = parseVersion(a);
  const vb = parseVersion(b);
  if (!va || !vb) return null;
  return (
    sign(va.epoch - vb.epoch) ||
    compareNumberLists(va.release, vb.release) ||
    compareNumberLists(preKey(va), preKey(vb)) ||
    sign((va.post ?? -Infinity) - (vb.post ?? -Infinity)) ||
    sign((va.dev ?? Infinity) - (vb.dev ?? Infinity)) ||
    compareLocal(va.local, vb.local)
  );
}

/**
 * A build no release should be offered to: a source checkout's git-derived
 * version (0.7.0a5.dev3+g1a2b3c4), a test build's (0.18.2.dev0), or any
 * local version. "dev" runs straight into its number, so no word boundary.
 */
export function isDevVersion(value) {
  return /\.?dev\d*|\+/i.test(String(value ?? "").trim());
}

// Fold a version into one spelling, used only when the running version cannot
// be parsed: the check then falls back to the old "differs from latest" test.
function canonicalVersion(value) {
  return String(value ?? "")
    .trim()
    .replace(/^v/i, "")
    .toLowerCase()
    .replace(/[-_]/g, "")
    .replace(/alpha/g, "a")
    .replace(/beta/g, "b")
    .replace(/preview|pre/g, "rc")
    .replace(/(a|b|rc)\.?(\d)/g, "$1$2");
}

/**
 * Whether `latest` (the release GitHub calls latest) is an update for
 * `current` (the running build).
 *
 * - Both readable: only when latest is strictly newer. Equal or older is
 *   never an update, which is what keeps a release candidate or any build
 *   ahead of the public release from being offered a downgrade.
 * - Latest unreadable: never. A tag this code cannot order is not something
 *   to put in front of every user; the card staying quiet costs one missed
 *   prompt, a wrong card can offer a downgrade.
 * - Current unreadable: the old test, latest differs from current. The
 *   running version comes from our own build, so an unreadable one is a
 *   packaging fault, and that user still needs to hear about a release.
 */
export function isNewerRelease(latest, current) {
  if (!parseVersion(latest)) return false;
  if (!parseVersion(current)) return canonicalVersion(latest) !== canonicalVersion(current);
  return compareVersions(latest, current) === 1;
}
