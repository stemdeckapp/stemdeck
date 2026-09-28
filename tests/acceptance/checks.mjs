// The checks of the manual test run for PR #713, with the same ids, titles and
// issue numbers as the manual checklist, so an automated result maps 1:1 onto
// a manual one. The steps and expected results live beside each test in
// acceptance.spec.mjs.

export const BUILD_LABEL = "Windows NVIDIA · PR #713";

export const CHECKS = [
  { id: "S1", area: "Setup", title: "The test build starts on its own, apart from the installed app", refs: [] },
  { id: "S2", area: "Setup", title: "The version is the test build's", refs: [] },
  { id: "S3", area: "Setup", title: "The run is isolated from your real library", refs: [] },
  { id: "A1", area: "Settings: song identification", title: "A key AcoustID refuses is not saved, and Settings says which key to use", refs: [712] },
  { id: "A2", area: "Settings: song identification", title: "An application key is checked and saved, and only its last two characters show", refs: [706, 712] },
  { id: "A3", area: "Settings: song identification", title: "Transcribe lyrics offers Auto, On and Off", refs: [702] },
  { id: "N1", area: "Now playing and song details", title: "The top bar is one extraction row with the now-playing card at its end", refs: [699] },
  { id: "N2", area: "Now playing and song details", title: "A musical's song names the show and the performer", refs: [704, 706, 707] },
  { id: "N3", area: "Now playing and song details", title: "A film song names the film", refs: [707] },
  { id: "N4", area: "Now playing and song details", title: "Official links appear only when they exist, and open outside the app", refs: [704] },
  { id: "N5", area: "Now playing and song details", title: "An English band gets its history, members and albums", refs: [704, 706] },
  { id: "N6", area: "Now playing and song details", title: "With a key saved, imports use the fingerprint", refs: [706] },
  { id: "L1", area: "Lyrics", title: "Lyrics follow the singer, karaoke style, and a line click jumps there", refs: [702] },
  { id: "L2", area: "Lyrics", title: "Polish lyrics get their letters back", refs: [703] },
  { id: "L3", area: "Lyrics", title: "A track with nothing to go on shows no lyrics rather than a guess", refs: [702] },
  { id: "C1", area: "Other languages", title: "A Chinese song finds its artist, native name and character-by-character karaoke", refs: [705, 709] },
  { id: "C2", area: "Other languages", title: "A Japanese song finds its artist and lyrics", refs: [705, 709] },
  { id: "C3", area: "Other languages", title: "A Korean song filed under its English title still gets lyrics", refs: [705, 709] },
  { id: "C4", area: "Other languages", title: "A compilation names its artist with no typing", refs: [709] },
  { id: "C5", area: "Other languages", title: "The artist box follows the app's language", refs: [709] },
  { id: "T1", area: "Timeline", title: "An hour-long track's ruler stays readable", refs: [710] },
  { id: "E1", area: "Errors and cancel", title: "A failed import shows a readable card you can close", refs: [708] },
  { id: "E2", area: "Errors and cancel", title: "Try again goes back to the link field", refs: [708] },
  { id: "E3", area: "Errors and cancel", title: "Escape closes the error alone, not the box beneath it", refs: [708] },
  { id: "E4", area: "Errors and cancel", title: "A lyrics error stays in the Lyrics panel", refs: [708] },
  { id: "E5", area: "Errors and cancel", title: "A cancelled import lets the next one start", refs: [706] },
  { id: "E6", area: "Errors and cancel", title: "Offline, an import still finishes", refs: [706, 702] },
  { id: "V1", area: "Fixes since 0.18.1 (#700)", title: "The now-playing card's date wraps instead of being cut off", refs: [698] },
  { id: "V2", area: "Fixes since 0.18.1 (#700)", title: "Unsorted has no grip, no New subfolder and no hover highlight", refs: [695] },
  { id: "V3", area: "Fixes since 0.18.1 (#700)", title: "The click track options box is solid with no song loaded", refs: [697] },
  { id: "V4", area: "Fixes since 0.18.1 (#700)", title: "Count-in says it plays with the click off", refs: [696] },
];

export const byId = (id) => CHECKS.find((c) => c.id === id);

/** A test title: the id, then the check's own title. */
export const title = (id) => `${id} ${byId(id).title}`;
