// Before any check: reset the package, start the real app and wait until the
// studio is on screen. The function returned is the teardown, which Playwright
// runs after the last check, also when checks failed: it closes the app this
// run started, and only that one.

import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "@playwright/test";
import {
  FAKE_LOCALAPPDATA,
  RESULTS_DIR,
  connect,
  launchApp,
  processTree,
  readState,
  resetPackageState,
  stopRecordedApp,
  target,
  writeState,
} from "./harness.mjs";

/** Size, time and hash of the real settings mirror, to prove it was not touched. */
export function fingerprint(file) {
  try {
    const bytes = fs.readFileSync(file);
    const stat = fs.statSync(file);
    return {
      exists: true,
      size: stat.size,
      mtimeMs: stat.mtimeMs,
      sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    };
  } catch {
    return { exists: false };
  }
}

export const realMirrorPath = () =>
  path.join(process.env.LOCALAPPDATA || "", "StemDeck", "settings.json");

export default async function globalSetup() {
  if (process.env.CI) {
    throw new Error("The acceptance run drives real services and a real build; it never runs in CI.");
  }
  fs.rmSync(RESULTS_DIR, { recursive: true, force: true });
  fs.mkdirSync(RESULTS_DIR, { recursive: true });
  const t = target();
  const runStartedAt = Date.now();

  if (t.mode === "server") {
    writeState({ mode: "server", url: t.url, baseURL: t.url, runStartedAt });
    const res = await fetch(`${t.url}/api/health`).catch((err) => ({ ok: false, status: String(err) }));
    if (!res.ok) throw new Error(`No StemDeck answers at ${t.url} (${res.status})`);
    return async () => {};
  }

  if (process.platform !== "win32") {
    throw new Error("The desktop target drives the Windows portable build. Use STEMDECK_ACCEPTANCE_TARGET=\"server <url>\" elsewhere.");
  }

  // A previous run that was killed may have left its app behind.
  await stopRecordedApp();
  const realMirror = fingerprint(realMirrorPath());
  resetPackageState();
  writeState({ mode: "desktop", runStartedAt, realMirror, jobs: {} });

  const app = await launchApp({ label: "app" });
  // A last resort if the runner itself is killed: the tree recorded at launch,
  // taken down synchronously. The backend also exits by itself once its
  // parent is gone (STEMDECK_PARENT_PID).
  process.once("exit", () => {
    const state = readState();
    if (state?.pid) spawnSync("taskkill.exe", ["/PID", String(state.pid), "/T", "/F"], { stdio: "ignore" });
  });

  try {
    // First run downloads CUDA torch (2.5 GB), FFmpeg and the models, so the
    // wait here is long. Later runs reach the studio in well under a minute.
    const { browser, page } = await connect(chromium, { timeout: 60 * 60_000 });
    await page.locator("#url").waitFor({ timeout: 5 * 60_000 });
    const studioAt = Date.now();
    const setupLog = fs.readFileSync(path.join(RESULTS_DIR, "app-stdout.log"), "utf8").slice(-4000);
    writeState({
      ...readState(),
      studioAt,
      startupSec: Math.round((studioAt - app.startedAt) / 1000),
      tree: processTree(app.pid).map((r) => ({ pid: r.p, exe: r.e })),
      setupLogTail: setupLog,
    });
    await browser.close();
  } catch (err) {
    await stopRecordedApp();
    throw err;
  }

  return async () => {
    await stopRecordedApp();
    const state = readState() || {};
    writeState({
      ...state,
      isolation: {
        realMirrorBefore: state.realMirror,
        realMirrorAfter: fingerprint(realMirrorPath()),
        fakeMirror: fingerprint(path.join(FAKE_LOCALAPPDATA, "StemDeck", "settings.json")).exists,
      },
    });
  };
}
