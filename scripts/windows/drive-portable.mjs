// Drives a running portable StemDeck through one ordinary session, for
// check-portable-leaks.ps1: first-run setup (with "skip", the AI model step is
// skipped and each model downloads when first needed, the way a real Split
// fetches it; with "warmup", the step runs in full), Settings opened, Export
// logs pressed, and one short WAV imported and split.
//
// It reaches the window through the WebView2 DevTools port the leak check
// opens with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS. It never quits the app:
// the leak check closes the window itself, so the shutdown is the normal one.
//
// Usage: node scripts/windows/drive-portable.mjs <devtools port> <app pid> <wav file> [minutes] [skip|warmup]

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { basename } from "node:path";
import { chromium } from "@playwright/test";

const [port, appPid, wav, minutes = "20", models = "skip"] = process.argv.slice(2);
if (!port || !appPid || !wav || !["skip", "warmup"].includes(models)) {
  console.error("usage: drive-portable.mjs <devtools port> <app pid> <wav file> [minutes] [skip|warmup]");
  process.exit(2);
}

// Closes (cancels) every visible standard dialog of the app's process: the
// native save dialog is not part of the page, so DevTools cannot reach it.
function closeSaveDialogs(pid) {
  const script = `
Add-Type @'
using System; using System.Runtime.InteropServices; using System.Text;
public static class SaveDialogs {
  delegate bool EnumProc(IntPtr window, IntPtr data);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr data);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr window, StringBuilder name, int size);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr window);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr window, uint message, IntPtr w, IntPtr l);
  public static int Close(uint target) {
    int closed = 0;
    EnumWindows((window, data) => {
      uint pid; GetWindowThreadProcessId(window, out pid);
      var name = new StringBuilder(64); GetClassName(window, name, 64);
      if (pid == target && name.ToString() == "#32770" && IsWindowVisible(window)) {
        PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero); closed++;
      }
      return true;
    }, IntPtr.Zero);
    return closed;
  }
}
'@
[SaveDialogs]::Close(${Number(pid)})`;
  const result = spawnSync("powershell", ["-NoProfile", "-NonInteractive", "-Command", script], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(`closing the save dialog failed: ${result.stderr}`);
  return Number(result.stdout.trim());
}
const setupButtons = models === "skip" ? ["#skip-models", "#continue"] : ["#continue"];
const deadline = Date.now() + Number(minutes) * 60_000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const say = (line) => console.log(`[drive] ${line}`);

function remaining() {
  const left = deadline - Date.now();
  if (left <= 0) throw new Error("timed out");
  return left;
}

// WebView2 does not always open the DevTools port: on a first run with the
// profile redirected it stayed closed. Exit code 3 tells the leak check to
// run the session over HTTP instead.
async function connect() {
  const giveUp = Date.now() + 120_000;
  for (;;) {
    try {
      return await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    } catch {
      remaining();
      if (Date.now() > giveUp) {
        say("the window's DevTools port did not open");
        process.exit(3);
      }
      await sleep(1000);
    }
  }
}

const browser = await connect();
say("connected to the window");
const page = browser.contexts()[0].pages()[0];

// Setup ends with the window moving to the backend's own address.
let settingsReady = false;
while (!settingsReady) {
  remaining();
  if (page.url().startsWith("http://127.0.0.1:")) {
    settingsReady = await page.locator("#settingsBtn").isVisible().catch(() => false);
  } else {
    for (const id of setupButtons) {
      const button = page.locator(id);
      if ((await button.isVisible().catch(() => false)) && (await button.isEnabled().catch(() => false))) {
        say(`setup: pressing ${id}`);
        await button.click().catch(() => {});
      }
    }
    if (await page.locator("#retry").isVisible().catch(() => false)) {
      const status = await page.locator("#status").textContent().catch(() => "");
      throw new Error(`setup failed: ${status}`);
    }
  }
  await sleep(1000);
}
say(`studio open at ${page.url()}`);

await page.locator("#settingsBtn").click();
await page.locator(".settings-tab").first().waitFor({ state: "visible", timeout: remaining() });
say("Settings opened");

// Export logs: the desktop app answers with its native save dialog, which is
// closed here so nothing is saved. A build that lets the window download the
// zip by itself leaves it in Playwright's folder, where the leak check finds it.
await page.locator('.settings-tab[data-tab="logs"]').click();
await page.locator(".settings-export-logs").click();
await sleep(5000);
say(`Export logs pressed, save dialogs closed: ${closeSaveDialogs(appPid)}`);
await page.keyboard.press("Escape");

const bytes = [...readFileSync(wav)];
const jobId = await page.evaluate(
  async ({ bytes, name }) => {
    const form = new FormData();
    form.append("file", new Blob([new Uint8Array(bytes)], { type: "audio/wav" }), name);
    const response = await fetch("/api/jobs", { method: "POST", body: form });
    if (!response.ok) throw new Error(`import refused: ${response.status} ${await response.text()}`);
    return (await response.json()).job_id;
  },
  { bytes, name: basename(wav) },
);
say(`imported ${basename(wav)} as job ${jobId}`);

const terminal = new Set(["done", "error", "failed", "cancelled", "stopped"]);
let status = "";
while (!terminal.has(status)) {
  remaining();
  await sleep(3000);
  const job = await page.evaluate(async (id) => (await fetch(`/api/jobs/${id}`)).json(), jobId);
  if (job.status !== status) say(`job ${jobId}: ${job.status} ${job.stage ?? ""}`.trim());
  status = job.status;
}
// No browser.close(): the window stays up for the leak check to close.
process.exit(status === "done" ? 0 : 1);
