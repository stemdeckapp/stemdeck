import { LANGUAGES, TRANSLATIONS } from "../../static/js/i18n.js";
import {
  destroySections,
  flushSectionsSave,
  initSections,
  sectionDisplayName,
} from "../../static/js/sections.js";

let pass = 0;
let fail = 0;
const check = (name, condition) => {
  if (condition) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}`);
  }
};

const sectionKeys = [
  "sections.suggested",
  "sections.kind.intro",
  "sections.kind.outro",
  "sections.kind.break",
  "sections.kind.bridge",
  "sections.kind.inst",
  "sections.kind.solo",
  "sections.kind.verse",
  "sections.kind.chorus",
  "sections.kind.part",
  "sections.kindNumbered",
];

for (const { code } of LANGUAGES) {
  const table = code === "pt-PT" ? TRANSLATIONS.pt : TRANSLATIONS[code];
  check(`${code} has every automatic-section label`, sectionKeys.every((key) => table[key]));
}
check(
  "English automatic-section badge explains that adjustment is experimental",
  TRANSLATIONS.en["sections.suggested"] === "Experimental - drag to adjust.",
);

check(
  "canonical kinds use translated display labels",
  sectionDisplayName({ kind: "chorus", name: "model-label" }) === "Chorus",
);
check(
  "custom names remain unchanged",
  sectionDisplayName({ name: "Pre-Chorus" }) === "Pre-Chorus",
);

// The model can label two adjacent spans with one kind and still be marking a
// real structural change, so the boundary is kept and the labels are numbered.
const repeated = [
  { id: "a", kind: "chorus", start: 0, end: 10 },
  { id: "b", kind: "verse", start: 10, end: 20 },
  { id: "c", kind: "chorus", start: 20, end: 30 },
];
check(
  "a repeated kind is numbered in running order",
  sectionDisplayName(repeated[0], repeated) === "Chorus 1" &&
    sectionDisplayName(repeated[2], repeated) === "Chorus 2",
);
check(
  "a kind used once is never numbered",
  sectionDisplayName(repeated[1], repeated) === "Verse",
);
check(
  "numbering follows time, not list order",
  sectionDisplayName(repeated[2], [repeated[2], repeated[1], repeated[0]]) === "Chorus 2",
);
check(
  "a renamed section keeps its own name even beside repeated kinds",
  sectionDisplayName({ id: "d", name: "Pre-Chorus", start: 5, end: 6 }, repeated) === "Pre-Chorus",
);
check(
  "every language orders the numbered label around its own kind word",
  LANGUAGES.every(({ code }) => {
    const table = code === "pt-PT" ? TRANSLATIONS.pt : TRANSLATIONS[code];
    const value = table["sections.kindNumbered"];
    return value.includes("{kind}") && value.includes("{n}");
  }),
);

const badge = {
  hidden: true,
  classList: {
    toggle(_name, force) {
      badge.hidden = force;
    },
  },
};
globalThis.document = {
  getElementById(id) {
    return id === "sectionsSuggested" ? badge : null;
  },
};

initSections(
  "abcdefabcdef",
  [{ id: "auto-001", kind: "verse", name: "Verse", start: 0, end: 10, color: "#fff" }],
  10,
  "automatic",
);
check("automatic sections show the experimental adjustment badge", badge.hidden === false);
destroySections();
check("destroying sections hides the experimental adjustment badge", badge.hidden === true);

const requests = [];
const complete = [];
globalThis.fetch = (_url, options) => new Promise((resolve) => {
  requests.push(JSON.parse(options.body));
  complete.push(resolve);
});

initSections(
  "abcdefabcdef",
  [{ id: "auto-001", kind: "intro", name: "Intro", start: 0, end: 10, color: "#fff" }],
  20,
  "automatic",
);
const firstSave = flushSectionsSave();
await Promise.resolve();
initSections(
  "abcdefabcdef",
  [{ id: "auto-002", kind: "verse", name: "Verse", start: 10, end: 20, color: "#fff" }],
  20,
  "automatic",
);
const secondSave = flushSectionsSave();
await Promise.resolve();
check("a second section save waits for the first", requests.length === 1);
complete[0]({ ok: true });
await firstSave;
await Promise.resolve();
check("the newest snapshot starts after the first completes", requests.length === 2);
check("the first queued snapshot is preserved", requests[0].sections[0].id === "auto-001");
check("the second queued snapshot is preserved", requests[1].sections[0].id === "auto-002");
complete[1]({ ok: true });
await secondSave;
destroySections();

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
