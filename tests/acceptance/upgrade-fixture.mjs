// A library the way 0.20.0 left it, for UPG-01 and UPG-02.
//
// tests/fixtures/upgrade-0.20.0 holds what the 0.20.0 code wrote (make.py runs
// the tagged code): registry.json, the song's metadata.json, the desktop
// library store user-data.json, and the settings.json the desktop shell keeps
// in %LOCALAPPDATA%\StemDeck. This copies it into a throwaway folder under the
// repository's git-ignored data\ and adds what the repository does not carry:
// the stems, as real PCM16 WAVs, and their peaks.json, both where 0.20.0's
// pipeline put them.
//
// Layout of the copy, under `root`:
//
//   library\                 the old StemData location (jobs folder)
//   LocalAppData\StemDeck\settings.json   the 0.20.0 settings, naming library\
//
// Nothing here reads or writes outside `root`.

import fs from "node:fs";
import path from "node:path";
import { REPO_ROOT } from "./harness.mjs";

export const FIXTURE_DIR = path.join(REPO_ROOT, "tests", "fixtures", "upgrade-0.20.0");
export const UPG_JOB_ID = "0a20c0ffee01";
export const UPG_TITLE = "Old Song From 0.20.0";
export const UPG_FOLDER = "Practice 2026";
export const UPG_STEMS = ["vocals", "drums", "bass", "other"];
const SECONDS = 4;
const RATE = 44100;

/** The settings the 0.20.0 install had, exactly as make.py wrote them. */
export function oldSettings() {
  return JSON.parse(fs.readFileSync(path.join(FIXTURE_DIR, "settings.json"), "utf8"));
}

/** A stereo PCM16 tone, the format Demucs wrote stems in. */
function wav(freq) {
  const frames = RATE * SECONDS;
  const data = Buffer.alloc(frames * 4);
  for (let i = 0; i < frames; i++) {
    const v = Math.round(12000 * Math.sin((2 * Math.PI * freq * i) / RATE));
    data.writeInt16LE(v, i * 4);
    data.writeInt16LE(v, i * 4 + 2);
  }
  const head = Buffer.alloc(44);
  head.write("RIFF", 0, "ascii");
  head.writeUInt32LE(36 + data.length, 4);
  head.write("WAVE", 8, "ascii");
  head.write("fmt ", 12, "ascii");
  head.writeUInt32LE(16, 16);
  head.writeUInt16LE(1, 20);
  head.writeUInt16LE(2, 22);
  head.writeUInt32LE(RATE, 24);
  head.writeUInt32LE(RATE * 4, 28);
  head.writeUInt16LE(4, 32);
  head.writeUInt16LE(16, 34);
  head.write("data", 36, "ascii");
  head.writeUInt32LE(data.length, 40);
  return Buffer.concat([head, data]);
}

/** Min and max per bucket, the shape the pipeline's peaks.json has. */
function peaks(points = 400) {
  return Array.from({ length: points }, (_, i) => {
    const a = Math.round(Math.abs(Math.sin(i / 18)) * 0.8 * 1e4) / 1e4;
    return [-a, a];
  });
}

/**
 * Copy the fixture into `root` (emptied first). Returns the paths the test
 * needs: the library folder and the LOCALAPPDATA to start the build with.
 */
export function prepareUpgradeLibrary(root) {
  const resolved = path.resolve(root);
  const dataRoot = path.join(REPO_ROOT, "data") + path.sep;
  if (!resolved.startsWith(dataRoot)) {
    throw new Error(`The upgrade library goes under ${dataRoot}, not ${resolved}.`);
  }
  fs.rmSync(resolved, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
  const library = path.join(resolved, "library");
  const localAppData = path.join(resolved, "LocalAppData");
  fs.cpSync(path.join(FIXTURE_DIR, "library"), library, { recursive: true });

  const stems = path.join(library, UPG_JOB_ID, "stems");
  fs.mkdirSync(stems, { recursive: true });
  UPG_STEMS.forEach((name, i) => fs.writeFileSync(path.join(stems, `${name}.wav`), wav(220 * (i + 1))));
  fs.writeFileSync(path.join(stems, "peaks.json"), JSON.stringify(Object.fromEntries(UPG_STEMS.map((n) => [n, peaks()]))));
  // The upload 0.20.0 kept beside a finished job.
  fs.writeFileSync(path.join(library, UPG_JOB_ID, "source.wav"), wav(220));

  const settings = { ...oldSettings(), jobs_dir: library };
  fs.mkdirSync(path.join(localAppData, "StemDeck"), { recursive: true });
  fs.writeFileSync(path.join(localAppData, "StemDeck", "settings.json"), JSON.stringify(settings));
  return { root: resolved, library, localAppData, settings };
}
