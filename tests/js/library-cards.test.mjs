// static/js/shared/jobs.js -- the DOM-free half of the library.
//
// Nothing imported this module before. It is what turns an /api/jobs response
// into the rows the mobile UI draws, and it is shared with the desktop catalog,
// so a mistake here shows up in two places at once and in neither backend test.
//
// The interesting parts are the classifications: which status counts as
// "working", what a source URL is called on screen, and the requirement that a
// track's cover colour never changes between sessions.
//
// Run:  node tests/js/library-cards.test.mjs

import {
  coverGradient,
  coverInitial,
  deriveSource,
  fetchJobs,
  jobToCard,
  statusKind,
} from "../../static/js/shared/jobs.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const eq = (name, actual, expected) =>
  check(
    name,
    Object.is(actual, expected),
    `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`,
  );

// ── deriveSource: what the card says a track came from ─────────────────────
{
  eq("a watch URL is YouTube", deriveSource("https://www.youtube.com/watch?v=abc"), "YouTube");
  eq("a short link is YouTube", deriveSource("https://youtu.be/abc"), "YouTube");
  eq("music.youtube is YouTube", deriveSource("https://music.youtube.com/watch?v=a"), "YouTube");
  eq("a set is SoundCloud", deriveSource("https://soundcloud.com/x/sets/y"), "SoundCloud");
  eq("an upload is a local file", deriveSource("local:My Song"), "Local file");

  // A job created before source_url existed has none; it is still an upload as
  // far as the card is concerned, not an unlabelled "Web" row.
  eq("a job with no source is a local file", deriveSource(null), "Local file");
  eq("an empty source is a local file", deriveSource(""), "Local file");
  eq("an undefined source is a local file", deriveSource(undefined), "Local file");

  eq("anything else is just the web", deriveSource("https://example.com/track"), "Web");
}

// ── statusKind: which dot the row gets ─────────────────────────────────────
{
  for (const s of ["queued", "downloading", "analyzing", "separating", "processing"]) {
    eq(`${s} is still working`, statusKind(s), "processing");
  }
  eq("done is done", statusKind("done"), "done");

  // Everything else has to land on "unavailable" rather than falling through
  // as undefined -- the row still needs a dot to render.
  for (const s of ["error", "cancelled", "unavailable", "", null, undefined, "nonsense"]) {
    eq(`${JSON.stringify(s)} is unavailable`, statusKind(s), "unavailable");
  }
}

// ── coverInitial ───────────────────────────────────────────────────────────
{
  eq("the first letter, capitalised", coverInitial("daft punk"), "D");
  eq("already capitalised stays put", coverInitial("Zebra"), "Z");
  eq("leading space is ignored", coverInitial("  moon"), "M");
  eq("a digit is fine", coverInitial("99 problems"), "9");

  // An untitled track still needs something in the square.
  eq("no title falls back to a note", coverInitial(""), "♪");
  eq("whitespace only falls back to a note", coverInitial("   "), "♪");
  eq("null falls back to a note", coverInitial(null), "♪");
  eq("undefined falls back to a note", coverInitial(undefined), "♪");

  // Non-Latin titles must not produce an empty square.
  check("a non-Latin title still yields a character", coverInitial("日本語").length > 0);
}

// ── coverGradient: the same track is always the same colour ────────────────
{
  const a = coverGradient("abcdefabcdef");
  check("a seed yields a gradient", typeof a === "string" && a.startsWith("linear-gradient"));
  eq("the same seed yields the same gradient", coverGradient("abcdefabcdef"), a);

  // This is the whole point: the library is re-rendered on every load, and a
  // track that changed colour between sessions would read as a different track.
  check(
    "the mapping is stable across many seeds",
    ["a", "b", "song-1", "song-2", "0123456789ab"].every(
      (s) => coverGradient(s) === coverGradient(s),
    ),
  );

  // A missing id must not throw on the way to a colour.
  check("no seed still yields a gradient", coverGradient(null).startsWith("linear-gradient"));
  check("an empty seed still yields a gradient", coverGradient("").startsWith("linear-gradient"));

  // The hash should actually spread; a formula that collapsed to one colour
  // would still pass every test above.
  const spread = new Set(
    Array.from({ length: 200 }, (_, i) => coverGradient(`job-${i}`)),
  );
  check("different tracks get different colours", spread.size >= 5, `${spread.size} distinct`);
}

// ── jobToCard: the shape the UI renders ────────────────────────────────────
{
  const card = jobToCard({
    job_id: "abcdefabcdef",
    title: "Get Lucky",
    status: "done",
    duration: 125,
    stems: ["vocals", "drums"],
    source_url: "https://youtu.be/abc",
    created_at: 1700000000,
    thumbnail: "https://img/thumb.jpg",
    selected_stems: ["vocals"],
  });

  eq("the id comes across", card.id, "abcdefabcdef");
  eq("the title comes across", card.title, "Get Lucky");
  eq("the source is derived", card.sub, "YouTube");
  eq("the status is classified", card.status, "done");
  eq("the stem count is counted", card.stemCount, 2);
  eq("the meta line pairs duration and stems", card.meta, "02:05 · 2 stems");
  eq("the thumbnail comes across", card.thumb, "https://img/thumb.jpg");
  eq("createdAt comes across", card.createdAt, 1700000000);

  // Carried through rather than only derived, so an unavailable card can be
  // reimported without a second fetch.
  eq("the source URL is carried", card.sourceUrl, "https://youtu.be/abc");
  check("the selected stems are carried", card.selectedStems.length === 1);
}

{
  // One stem is "1 stem", not "1 stems".
  const one = jobToCard({ job_id: "a", duration: 60, stems: ["vocals"], status: "done" });
  eq("a single stem is singular", one.meta, "01:00 · 1 stem");
}

{
  // A job still downloading has no duration and no stems yet: the meta line
  // must come out empty rather than "undefined" or " · ".
  const pending = jobToCard({ job_id: "a", title: "Pending", status: "downloading" });
  eq("a job with nothing known yet has an empty meta line", pending.meta, "");
  eq("and is shown as working", pending.status, "processing");
  eq("and has no thumbnail rather than undefined", pending.thumb, "");
  eq("and no source URL rather than undefined", pending.sourceUrl, null);
}

{
  // A completely empty state object must still produce a renderable card.
  const empty = jobToCard({});
  eq("an untitled job gets a placeholder title", empty.title, "Untitled");
  eq("and a fallback cover letter", empty.initial, "U");
  eq("and a createdAt of zero rather than undefined", empty.createdAt, 0);
  eq("and is not claimed to be done", empty.status, "unavailable");
  check("and still has a gradient", empty.gradient.startsWith("linear-gradient"));
}

{
  // selected_stems is the fallback when stems has not been populated yet.
  const sel = jobToCard({ job_id: "a", selected_stems: ["vocals", "drums", "bass"] });
  eq("selected stems are counted when stems is absent", sel.stemCount, 3);
}

{
  // A thumbnail that is not a string (an object from a malformed payload)
  // must not reach an <img src>.
  const bad = jobToCard({ job_id: "a", thumbnail: { url: "x" } });
  eq("a non-string thumbnail is dropped", bad.thumb, "");
}

// ── fetchJobs ──────────────────────────────────────────────────────────────
{
  const originalFetch = globalThis.fetch;

  globalThis.fetch = async (url, opts) => {
    check("the library is never served from cache", opts?.cache === "no-store");
    eq("it asks the canonical endpoint", url, "/api/jobs");
    return { ok: true, status: 200, json: async () => [{ job_id: "a" }] };
  };
  const rows = await fetchJobs();
  check("a successful fetch returns the rows", Array.isArray(rows) && rows.length === 1);

  // A failing request must reject rather than resolve to undefined, which the
  // caller would render as an empty library -- indistinguishable from "you
  // have no tracks".
  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}) });
  let threw = false;
  try {
    await fetchJobs();
  } catch (e) {
    threw = /503/.test(String(e.message));
  }
  check("a failed fetch throws rather than looking like an empty library", threw);

  globalThis.fetch = originalFetch;
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
