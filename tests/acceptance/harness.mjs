// The app under test: where it is, how its state is reset, and how it is
// started, reached and stopped.
//
// Two targets:
//
//   desktop (default)  the real portable build, StemDeck.exe, driven over the
//                      Chrome DevTools Protocol its WebView2 opens when
//                      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS asks for it.
//   server <url>       an already running backend (a dev server), driven in
//                      Playwright's own Chromium. Nothing is reset or stopped.
//
// Isolation of the desktop build is the point of most of this file. A portable
// package is not isolated by default: ensure_workspace restores settings.json
// (and with it jobs_dir) from %LOCALAPPDATA%\StemDeck, and the backend mirrors
// every setting change back there (STEMDECK_SETTINGS_MIRROR). So the build is
// started with LOCALAPPDATA pointed at a throwaway folder, its own data folder
// is given a settings.json naming its own jobs folder, and the WebView2
// profile is put in the throwaway folder too. The user's real
// %LOCALAPPDATA%\StemDeck and library are never read or written.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

export const PACKAGE_DIR = path.resolve(
  process.env.STEMDECK_ACCEPTANCE_PACKAGE || path.join(REPO_ROOT, "dist", "StemDeck-Windows-x64.NVIDIA"),
);
export const EXE = path.join(PACKAGE_DIR, "StemDeck.exe");
export const DATA_DIR = path.join(PACKAGE_DIR, "data");
export const JOBS_DIR = path.join(DATA_DIR, "jobs");
export const LOGS_DIR = path.join(DATA_DIR, "logs");

// Throwaway, outside the repo: the WebView2 profile alone is tens of MB.
export const WORK_DIR = path.join(os.tmpdir(), "stemdeck-acceptance");
export const FAKE_LOCALAPPDATA = path.join(WORK_DIR, "LocalAppData");
const STATE_FILE = path.join(WORK_DIR, "state.json");

// Gitignored. Not under test-results/: every run of the normal e2e suite wipes
// that folder, and the report would go with it.
export const RESULTS_DIR = path.join(REPO_ROOT, "acceptance-results");
export const SHOTS_DIR = path.join(RESULTS_DIR, "screenshots");
export const FILES_DIR = path.join(RESULTS_DIR, "files");

/** "desktop", or "server" with its URL, from STEMDECK_ACCEPTANCE_TARGET. */
export function target() {
  const raw = (process.env.STEMDECK_ACCEPTANCE_TARGET || "desktop").trim();
  if (raw === "desktop") return { mode: "desktop" };
  const url = raw.replace(/^server\s+/, "");
  if (!/^https?:\/\//.test(url)) {
    throw new Error(`STEMDECK_ACCEPTANCE_TARGET must be "desktop" or "server <url>", not "${raw}"`);
  }
  return { mode: "server", url: url.replace(/\/+$/, "") };
}

export const readState = () => {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
  } catch {
    return null;
  }
};

export const writeState = (state) => {
  fs.mkdirSync(WORK_DIR, { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
};

const rm = (p) => fs.rmSync(p, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });

/**
 * Put the package back to a first-import state, keeping what is slow to get.
 *
 * Removed: the library (jobs, registry, the desktop library store, which lives
 * in the jobs folder as user-data.json), settings.json, the run logs, the
 * MusicBrainz answer cache (so every run asks the real service), rendered
 * mixdowns, and the whole throwaway LOCALAPPDATA with its WebView2 profile.
 * Kept: data\ffmpeg, the package's python with CUDA torch, the models folder
 * (Demucs and Whisper), the downloaded runtime archives and config.json.
 */
export function resetPackageState() {
  if (!fs.existsSync(EXE)) throw new Error(`No build at ${EXE}. Set STEMDECK_ACCEPTANCE_PACKAGE.`);
  if (!fs.existsSync(path.join(PACKAGE_DIR, "portable.txt"))) {
    // Without the marker the build keeps its data in %LOCALAPPDATA%\StemDeck,
    // which here would be the throwaway folder, but its jobs default to
    // Documents\StemDeck\jobs: possibly a real library. Refuse.
    throw new Error(`${PACKAGE_DIR} is not a portable package (no portable.txt).`);
  }
  // The desktop store is copied from Documents\StemDeck\user-data.json into a
  // new jobs folder once (documents_store_path). That file would be the user's
  // real library layout, so refuse rather than import it into the test run.
  const docs = documentsDir();
  if (docs && fs.existsSync(path.join(docs, "StemDeck", "user-data.json"))) {
    throw new Error(
      `${path.join(docs, "StemDeck", "user-data.json")} exists; the build would copy that library `
      + "into the test run. Move it aside for the run.",
    );
  }
  fs.mkdirSync(DATA_DIR, { recursive: true });
  rm(JOBS_DIR);
  fs.mkdirSync(JOBS_DIR, { recursive: true });
  rm(path.join(DATA_DIR, "settings.json"));
  for (const name of ["backend.log", "stemdeck.log"]) rm(path.join(LOGS_DIR, name));
  for (const name of ["musicbrainz", "mixdown", "click"]) rm(path.join(DATA_DIR, "cache", name));
  // jobs_dir names the package's own folder, so neither the Documents default
  // nor a restored setting can ever point the build at a real library.
  fs.writeFileSync(
    path.join(DATA_DIR, "settings.json"),
    JSON.stringify({ jobs_dir: JOBS_DIR }, null, 2),
  );
  rm(FAKE_LOCALAPPDATA);
  fs.mkdirSync(FAKE_LOCALAPPDATA, { recursive: true });
}

function documentsDir() {
  const out = spawnSync("powershell.exe", [
    "-NoProfile", "-Command", "[Environment]::GetFolderPath('MyDocuments')",
  ], { encoding: "utf8" });
  return out.stdout?.trim() || "";
}

export function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

/**
 * The environment the build is started with: the user's, minus anything that
 * would steer StemDeck elsewhere (every STEMDECK_ variable, including the
 * suite's own AcoustID key), plus the isolation and the debugging port.
 * `offline` adds a proxy nobody listens on, for this app alone (see E6).
 */
function appEnv(cdpPort, { offline = false } = {}) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (/^STEMDECK_/i.test(k) || /^WEBVIEW2_/i.test(k)) continue;
    if (offline && /^(https?|all|no)_proxy$/i.test(k)) continue;
    env[k] = v;
  }
  env.LOCALAPPDATA = FAKE_LOCALAPPDATA;
  env.WEBVIEW2_USER_DATA_FOLDER = path.join(FAKE_LOCALAPPDATA, "WebView2");
  // The three throttling switches keep timers and animation frames running
  // when the window is covered by another one during an unattended run: the
  // karaoke wipe is drawn per frame, and an occluded WebView2 stops drawing.
  const args = [
    `--remote-debugging-port=${cdpPort}`,
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
  ];
  if (offline) {
    // Port 9 (discard) has no listener: every outside request is refused at
    // once, which is what an unplugged cable looks like to the app, without
    // touching the machine's network. Loopback stays direct, so the page can
    // still reach its own backend (Chromium's "<-loopback>" would do the
    // opposite and send it through the proxy too).
    const proxy = "http://127.0.0.1:9";
    Object.assign(env, {
      HTTP_PROXY: proxy,
      HTTPS_PROXY: proxy,
      ALL_PROXY: proxy,
      NO_PROXY: "127.0.0.1,localhost,::1",
    });
    args.push(`--proxy-server=${proxy}`, "--proxy-bypass-list=127.0.0.1;localhost;[::1]");
  }
  env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = args.join(" ");
  return env;
}

/** Start StemDeck.exe and wait for its DevTools endpoint. Records the pid. */
export async function launchApp({ offline = false, label = "app" } = {}) {
  const cdpPort = Number(process.env.STEMDECK_ACCEPTANCE_CDP_PORT) || (await freePort());
  fs.mkdirSync(RESULTS_DIR, { recursive: true });
  const out = fs.openSync(path.join(RESULTS_DIR, `${label}-stdout.log`), "a");
  // cwd is load-bearing: app_root() looks for a repo root from the working
  // directory first, and from the repo root the shell would run the checkout's
  // backend with %LOCALAPPDATA%\StemDeck as its data folder.
  const child = spawn(EXE, [], {
    cwd: PACKAGE_DIR,
    env: appEnv(cdpPort, { offline }),
    stdio: ["ignore", out, out],
    windowsHide: false,
  });
  const startedAt = Date.now();
  writeState({ ...(readState() || {}), mode: "desktop", pid: child.pid, cdpPort, startedAt, offline });
  child.unref();

  const endpoint = `http://127.0.0.1:${cdpPort}`;
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`StemDeck.exe exited at once (code ${child.exitCode})`);
    try {
      const res = await fetch(`${endpoint}/json/version`);
      if (res.ok) return { pid: child.pid, cdpPort, endpoint, startedAt };
    } catch {
      // Not up yet.
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  await stopApp(child.pid);
  throw new Error(
    "WebView2 opened no DevTools endpoint. WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS was not honoured.",
  );
}

// ─── Processes ──────────────────────────────────────────────────────────────

/** Every process on the machine: pid, parent, start time, path, command line. */
function processTable() {
  const script = "Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{ p = $_.ProcessId; pp = $_.ParentProcessId; c = [string]$_.CreationDate; e = $_.ExecutablePath; l = $_.CommandLine } } | ConvertTo-Json -Compress";
  const out = spawnSync("powershell.exe", ["-NoProfile", "-Command", script], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  try {
    const rows = JSON.parse(out.stdout || "[]");
    return Array.isArray(rows) ? rows : [rows];
  } catch {
    return [];
  }
}

/** `pid` and everything started under it, as they are now. */
export function processTree(pid) {
  const rows = processTable();
  const byParent = new Map();
  for (const row of rows) {
    if (!byParent.has(row.pp)) byParent.set(row.pp, []);
    byParent.get(row.pp).push(row);
  }
  const root = rows.find((r) => r.p === pid);
  if (!root) return [];
  const tree = [root];
  for (let i = 0; i < tree.length; i++) {
    for (const child of byParent.get(tree[i].p) || []) {
      // A child cannot predate its parent; an older one is a reused pid.
      if (String(child.c) >= String(tree[i].c)) tree.push(child);
    }
  }
  return tree;
}

const alive = (pid) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

/**
 * Close the app this suite started, and only that one: its window first, so
 * the shell stops its backend the normal way, then whatever of the process
 * tree recorded at the start is still there. Other StemDeck processes on the
 * machine are never looked at.
 */
export async function stopApp(pid) {
  if (!pid || !alive(pid)) return;
  const tree = processTree(pid);
  const isOurs = (row) => {
    if (row.p === pid) return true;
    const exe = String(row.e || "").toLowerCase();
    const line = String(row.l || "").toLowerCase();
    return exe.startsWith(PACKAGE_DIR.toLowerCase())
      || line.includes(FAKE_LOCALAPPDATA.toLowerCase())
      || exe.endsWith("msedgewebview2.exe")
      || exe.endsWith("python.exe");
  };
  spawnSync("taskkill.exe", ["/PID", String(pid)], { stdio: "ignore" });
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline && alive(pid)) await new Promise((r) => setTimeout(r, 250));
  if (alive(pid)) spawnSync("taskkill.exe", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
  // The backend watches its parent and exits by itself; give it the moment.
  await new Promise((r) => setTimeout(r, 3000));
  const now = new Map(processTable().map((r) => [r.p, r]));
  for (const row of tree.filter(isOurs)) {
    const still = now.get(row.p);
    // Same pid and same start time: the very process, not a reused number.
    if (still && String(still.c) === String(row.c)) {
      spawnSync("taskkill.exe", ["/PID", String(row.p), "/T", "/F"], { stdio: "ignore" });
    }
  }
}

/** Stop whatever the state file says this suite started. Safe to call twice. */
export async function stopRecordedApp() {
  const state = readState();
  if (state?.mode === "desktop" && state.pid) await stopApp(state.pid);
  if (state) writeState({ ...state, pid: null });
}

// ─── Reaching the page ──────────────────────────────────────────────────────

/**
 * The studio page of the running app: the WebView2's page once the setup
 * screen has handed over to the backend, or a fresh page on the server.
 * Returns { browser, page, baseURL }.
 */
export async function connect(chromium, { timeout = 60 * 60_000 } = {}) {
  const state = readState();
  if (!state) throw new Error("The app is not running: global setup did not finish.");
  if (state.mode === "server") {
    const browser = await chromium.launch({ headless: process.env.STEMDECK_ACCEPTANCE_HEADED !== "1" });
    const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
    const page = await context.newPage();
    await page.goto(state.url, { waitUntil: "domcontentloaded" });
    return { browser, page, baseURL: state.url };
  }
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${state.cdpPort}`);
  const deadline = Date.now() + timeout;
  for (;;) {
    const page = browser.contexts().flatMap((c) => c.pages())
      .find((p) => /^http:\/\/127\.0\.0\.1:\d+\//.test(p.url()));
    if (page) {
      const baseURL = new URL(page.url()).origin;
      writeState({ ...readState(), baseURL });
      return { browser, page, baseURL };
    }
    if (Date.now() > deadline) throw new Error("The app never left its setup screen.");
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ─── The window ─────────────────────────────────────────────────────────────

/**
 * Resize the app's real window (desktop), or the viewport (server). The now-
 * playing card only shows in a window wider than 1460px, and some checks
 * narrow the window on purpose.
 */
export function resizeWindow(width, height) {
  const state = readState();
  if (state?.mode !== "desktop" || !state.pid) return false;
  const script = `
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Win {
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}
"@
$p = Get-Process -Id ${state.pid}
$h = $p.MainWindowHandle
if ($h -eq [IntPtr]::Zero) { exit 3 }
[Win]::ShowWindow($h, 9) | Out-Null
[Win]::SetWindowPos($h, [IntPtr]::Zero, 40, 40, ${width}, ${height}, 0x0014) | Out-Null
`;
  const out = spawnSync("powershell.exe", ["-NoProfile", "-Command", script], { encoding: "utf8" });
  return out.status === 0;
}

/**
 * Drag with the real Windows cursor (desktop only): SendInput through the
 * app's window, so WebView2 gets the same input a hand gives. `points` are
 * page (CSS) pixels, the first where the button goes down, the last where it
 * comes up. The cursor moves on the tester's screen while this runs. False
 * when there is no app window to drag in.
 */
export function osDrag(points, dpr = 1) {
  const state = readState();
  if (state?.mode !== "desktop" || !state.pid || points.length < 2) return false;
  const path = points.map(([x, y]) => `@(${Math.round(x * dpr)},${Math.round(y * dpr)})`).join(",");
  const script = `
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Mouse {
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
}
"@
[Mouse]::SetProcessDPIAware() | Out-Null
$h = (Get-Process -Id ${state.pid}).MainWindowHandle
if ($h -eq [IntPtr]::Zero) { exit 3 }
# An Alt tap lets this process bring the window to the front.
[Mouse]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [Mouse]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[Mouse]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 300
$o = New-Object Mouse+POINT
[Mouse]::ClientToScreen($h, [ref]$o) | Out-Null
$pts = @(${path})
[Mouse]::SetCursorPos($o.X + $pts[0][0], $o.Y + $pts[0][1]) | Out-Null
Start-Sleep -Milliseconds 150
[Mouse]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 150
for ($i = 1; $i -lt $pts.Count; $i++) {
  $a = $pts[$i - 1]; $b = $pts[$i]
  for ($s = 1; $s -le 8; $s++) {
    [Mouse]::SetCursorPos($o.X + $a[0] + ($b[0] - $a[0]) * $s / 8, $o.Y + $a[1] + ($b[1] - $a[1]) * $s / 8) | Out-Null
    Start-Sleep -Milliseconds 25
  }
}
Start-Sleep -Milliseconds 150
[Mouse]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
`;
  const out = spawnSync("powershell.exe", ["-NoProfile", "-Command", script], { encoding: "utf8" });
  return out.status === 0;
}
