// Which track sources the Split stems button may be pointed at (#635).
//
// The composer is an input that button submits, not a caption for the open
// track. An uploaded file's source is the synthetic "local:my song.mp3", and
// showing its bare filename armed the button with a string that can never
// resolve: pressing it POSTed "my song.mp3" as though it were a link.
//
// Run:  node tests/js/source-arming.test.mjs
import { isReimportableSource } from "../../static/js/utils.js";

let pass = 0,
  fail = 0;
const check = (name, cond) => {
  if (cond) pass++;
  else {
    fail++;
    console.log(`FAIL  ${name}`);
  }
};

// Links the importer can act on.
for (const url of [
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "https://soundcloud.com/artist/track",
  "http://example.com/a.mp3",
]) {
  check(`reimportable: ${url}`, isReimportableSource(url) === true);
}

// Uploaded files. Not reachable a second time, whatever the name looks like.
for (const source of [
  "local:my song.mp3",
  "local:e2e-fixture.wav",
  // A filename that happens to read like a URL is still a filename.
  "local:https___youtube.com.mp3",
]) {
  check(`not reimportable: ${source}`, isReimportableSource(source) === false);
}

// Absent is not reimportable, and must not throw on the way to saying so.
for (const empty of [undefined, null, ""]) {
  check(`absent: ${JSON.stringify(empty)}`, isReimportableSource(empty) === false);
}

// The prefix is a scheme, so it only counts at the start.
check("local: mid-string is a URL", isReimportableSource("https://x.com/local:a") === true);

console.log(fail ? `\n${fail} FAILED` : `\n${pass}/${pass} checks passed`);
process.exit(fail ? 1 : 0);
