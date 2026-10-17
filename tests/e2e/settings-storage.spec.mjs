// Settings that decide where things go and how much is accepted: the stems
// (StemData) location and its safety messages, the exports folder, the YouTube
// cookies file, the max track length, the playlist import limit, the port, and
// the log viewer.
//
// Moving the library is desktop-only on the server (POST answers 403 unless
// STEMDECK_DESKTOP=1), and this suite's backend is a server, so the stems
// location endpoints are answered here. Their server half (validation, the
// move, rollback of the setting) is covered in tests/test_stems_location.py.
// Everything else talks to the real backend, and every value changed is put
// back afterwards: the backend is shared by the whole suite.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { seedLibrary, stubTauri } from "./helpers.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// Inside the repository's git-ignored data/ folder, never a system temp dir.
const SCRATCH = path.resolve(HERE, "../../data/e2e-settings-storage");

let original;

test.beforeAll(async ({ request }) => {
  original = await (await request.get("/api/settings")).json();
});

test.afterEach(async ({ request }) => {
  await request.post("/api/settings", {
    data: {
      max_duration_sec: original.max_duration_sec,
      playlist_max_items: original.playlist_max_items,
      cookies_file: original.cookies_file || "",
    },
  });
});

async function openSettings(page, tab) {
  await seedLibrary(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
  if (tab) await page.locator(`.settings-tab[data-tab="${tab}"]`).click();
}

/** Answer the stems location endpoints as a desktop backend would. */
async function stubStemsLocation(page, { get, post }) {
  const posts = [];
  await page.route("**/api/settings/stems-location", async (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ json: typeof get === "function" ? get() : get });
    }
    posts.push(JSON.parse(route.request().postData() || "{}"));
    if (post === "abort") return route.abort("connectionrefused");
    return route.fulfill({ status: post.status || 200, json: post.json });
  });
  return posts;
}

const DESKTOP_GET = {
  path: "/Users/someone/Documents/StemDeck/jobs",
  bytes: 2.5 * 1024 ** 3,
  editable: true,
  is_default: true,
  restart_required: false,
  busy: false,
};

// ── StemData location ────────────────────────────────────────────────────────

test("a server deployment shows no StemData location and no empty Storage card", async ({ page }) => {
  // The real backend: not the desktop app, so the setting is not editable.
  await openSettings(page, "library");
  await expect(page.locator(".set-stems-location")).toHaveCount(0);
  await expect(page.locator(".settings-group-storage")).toHaveCount(0);
});

test("the desktop shows where the stems are and how much is there", async ({ page }) => {
  await stubStemsLocation(page, { get: DESKTOP_GET, post: { json: {} } });
  await openSettings(page, "library");
  await expect(page.locator(".stems-location-path")).toHaveAttribute("title", DESKTOP_GET.path);
  await expect(page.locator(".stems-location-size")).toHaveText("2.5 GB");
  await expect(page.locator(".stems-location-msg")).toHaveText("");
});

test("a refused folder shows the server's reason as an error and the path stays", async ({ page }) => {
  const reason = "Pick an empty folder, or one StemDeck already uses.";
  const posts = await stubStemsLocation(page, {
    get: DESKTOP_GET,
    post: { status: 422, json: { detail: reason } },
  });
  await openSettings(page, "library");
  page.once("dialog", (d) => d.accept("C:\\Users\\someone\\Desktop"));
  await page.locator(".set-stems-location").click();

  const msg = page.locator(".stems-location-msg");
  await expect(msg).toHaveText(reason);
  await expect(msg).toHaveClass(/\berror\b/);
  expect(posts).toEqual([{ path: "C:\\Users\\someone\\Desktop" }]);
  await expect(page.locator(".stems-location-path")).toHaveAttribute("title", DESKTOP_GET.path);
  await expect(page.locator(".set-stems-location")).toBeEnabled();
});

test("a move says how many items moved and asks for a restart", async ({ page }) => {
  let moved = false;
  const target = "D:\\StemDeckData";
  await stubStemsLocation(page, {
    get: () => (moved
      ? { ...DESKTOP_GET, path: target, bytes: 1024, is_default: false, restart_required: true }
      : DESKTOP_GET),
    post: { json: { path: target, moved_entries: 3, bytes_moved: 1024, restart_required: true, persisted: true } },
  });
  await openSettings(page, "library");
  page.once("dialog", (d) => { moved = true; d.accept(target); });
  await page.locator(".set-stems-location").click();

  const msg = page.locator(".stems-location-msg");
  await expect(msg).toHaveText("Moved 3 items. Restart StemDeck to finish switching over.");
  await expect(msg).toHaveClass(/\bok\b/);
  // Re-read from the server, not taken from the POST.
  await expect(page.locator(".stems-location-path")).toHaveAttribute("title", target);
  await expect(page.locator(".stems-location-size")).toHaveText("1.0 KB");
});

test("a move whose setting did not save warns that a restart would go back", async ({ page }) => {
  await stubStemsLocation(page, {
    get: DESKTOP_GET,
    post: { json: { path: "E:\\Stems", moved_entries: 1, bytes_moved: 5, restart_required: true, persisted: false } },
  });
  await openSettings(page, "library");
  page.once("dialog", (d) => d.accept("E:\\Stems"));
  await page.locator(".set-stems-location").click();

  const msg = page.locator(".stems-location-msg");
  await expect(msg).toContainText("Moved 1 item, but StemDeck could not save this as your new location");
  await expect(msg).toHaveClass(/\berror\b/);
});

test("cancelling the folder prompt moves nothing", async ({ page }) => {
  const posts = await stubStemsLocation(page, { get: DESKTOP_GET, post: { json: {} } });
  await openSettings(page, "library");
  page.once("dialog", (d) => d.dismiss());
  await page.locator(".set-stems-location").click();
  await expect(page.locator(".stems-location-msg")).toHaveText("");
  expect(posts).toEqual([]);
});

test("a server that cannot be reached is said plainly", async ({ page }) => {
  await stubStemsLocation(page, { get: DESKTOP_GET, post: "abort" });
  await openSettings(page, "library");
  page.once("dialog", (d) => d.accept("F:\\Stems"));
  await page.locator(".set-stems-location").click();
  await expect(page.locator(".stems-location-msg")).toHaveText("Could not reach the server.");
  await expect(page.locator(".set-stems-location")).toBeEnabled();
});

test("reopening Settings after a move, before the restart, still says to restart", async ({ page }) => {
  await stubStemsLocation(page, {
    get: { ...DESKTOP_GET, path: "D:\\StemDeckData", restart_required: true },
    post: { json: {} },
  });
  await openSettings(page, "library");
  await expect(page.locator(".stems-location-msg")).toHaveText("Restart StemDeck to finish switching over.");
});

// ── Exports folder (desktop only) ───────────────────────────────────────────

async function stubExportsFolder(page, { current, picked }) {
  await stubTauri(page);
  await page.addInitScript(([cur, pick]) => {
    const base = window.__TAURI__.core.invoke;
    window.__TAURI__.core.invoke = (cmd, args) => {
      if (cmd === "current_exports_dir") return Promise.resolve(cur);
      if (cmd === "pick_exports_folder") {
        return pick === "fail" ? Promise.reject(new Error("no dialog")) : Promise.resolve(pick);
      }
      return base(cmd, args);
    };
  }, [current, picked]);
}

test("the exports folder is not offered in a browser", async ({ page }) => {
  await openSettings(page, "library");
  await expect(page.locator(".exports-location-row")).toHaveCount(0);
});

test("the desktop shows the exports folder and takes a new one", async ({ page }) => {
  await stubExportsFolder(page, { current: "C:\\Users\\me\\Music\\StemDeck", picked: "E:\\Drag" });
  await openSettings(page, "library");
  const pathEl = page.locator(".exports-location-path");
  await expect(pathEl).toHaveText("C:\\Users\\me\\Music\\StemDeck");
  await page.locator(".set-exports-location").click();
  await expect(pathEl).toHaveText("E:\\Drag");
  await expect(pathEl).toHaveAttribute("title", "E:\\Drag");
});

test("a picker that fails says so and keeps the folder", async ({ page }) => {
  await stubExportsFolder(page, { current: "C:\\Exports", picked: "fail" });
  await openSettings(page, "library");
  await page.locator(".set-exports-location").click();
  const msg = page.locator(".exports-location-msg");
  await expect(msg).toHaveText("The folder picker could not be opened.");
  await expect(msg).toHaveClass(/\berror\b/);
  await expect(page.locator(".exports-location-path")).toHaveText("C:\\Exports");
});

test("a shell too old to know the exports folder hides the row", async ({ page }) => {
  await stubExportsFolder(page, { current: null, picked: null });
  await openSettings(page, "library");
  await expect(page.locator(".exports-location-row")).toHaveCount(0);
});

// ── General: cookies, max track length, playlist limit ──────────────────────

test("a cookies file that does not exist is refused with a message", async ({ page, request }) => {
  await openSettings(page, "general");
  const input = page.locator(".set-cookies-file");
  await input.fill(path.join(SCRATCH, "missing", "cookies.txt"));
  await input.press("Tab");
  await expect(page.locator(".cookies-file-msg")).toHaveText("File not found, or not readable.");
  expect((await (await request.get("/api/settings")).json()).cookies_file || "").toBe(
    original.cookies_file || "",
  );
});

test("a cookies file that exists is kept, and clearing it clears it", async ({ page, request }) => {
  fs.mkdirSync(SCRATCH, { recursive: true });
  const cookies = path.join(SCRATCH, "cookies.txt");
  fs.writeFileSync(cookies, "# Netscape HTTP Cookie File\n");
  await openSettings(page, "general");
  const input = page.locator(".set-cookies-file");

  await input.fill(cookies);
  await input.press("Tab");
  await expect.poll(async () => (await (await request.get("/api/settings")).json()).cookies_file)
    .toBe(cookies);
  await expect(page.locator(".cookies-file-msg")).toHaveText("");

  await input.fill("");
  await input.press("Tab");
  await expect.poll(async () => (await (await request.get("/api/settings")).json()).cookies_file || "")
    .toBe("");
  await expect(input).toHaveValue("");
});

test("max track length takes digits only and shows the server's ceiling", async ({ page, request }) => {
  await openSettings(page, "general");
  const input = page.locator(".set-max-duration");
  const max = Math.round(original.max_duration_max_sec / 60);
  await expect(page.locator(".set-max-duration-desc")).toHaveText(
    `Longest track accepted for processing, in minutes (max ${max}).`,
  );

  await input.fill("");
  await input.pressSequentially("9a9");
  await expect(input).toHaveValue("99");
  await input.press("Tab");
  // The server clamps and the field shows what it kept.
  await expect(input).toHaveValue(String(max));
  expect((await (await request.get("/api/settings")).json()).max_duration_sec).toBe(max * 60);
});

test("the playlist limit is capped at 200 before it is sent", async ({ page, request }) => {
  const sent = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && new URL(req.url()).pathname === "/api/settings") {
      sent.push(JSON.parse(req.postData() || "{}"));
    }
  });
  await openSettings(page, "general");
  const input = page.locator(".set-playlist-max");
  await input.fill("750");
  await input.press("Tab");
  await expect.poll(() => sent).toContainEqual({ playlist_max_items: 200 });
  await expect(input).toHaveValue("200");
  await expect.poll(async () => (await (await request.get("/api/settings")).json()).playlist_max_items)
    .toBe(200);
});

// ── Network: port ───────────────────────────────────────────────────────────

test("in a browser the port is read-only and the panel says why", async ({ page }) => {
  await openSettings(page, "network");
  const port = page.locator(".set-port");
  await expect(port).toBeDisabled();
  await expect(port).toHaveValue(String(original.port));
  await expect(page.locator(".settings-server-note")).toContainText("read-only in server mode");
});

test("on the desktop a port below 1024 is raised to 1024 before it is saved", async ({ page }) => {
  // Answered here: GET reports the port this server runs on, not the stored
  // preference, so the stored value is only visible in what is sent.
  const sent = [];
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = JSON.parse(route.request().postData() || "{}");
    sent.push(body);
    return route.fulfill({ json: { ...original, ...body } });
  });
  await stubTauri(page);
  await openSettings(page, "network");
  const port = page.locator(".set-port");
  await expect(port).toBeEnabled();
  await port.fill("80");
  await port.press("Tab");
  await expect(port).toHaveValue("1024");
  expect(sent).toContainEqual({ port: 1024 });
});

// ── Logs ────────────────────────────────────────────────────────────────────

test("the log location lists where the logs are", async ({ page, request }) => {
  const info = await (await request.get("/api/logs")).json();
  await openSettings(page, "logs");
  await expect(page.locator(".settings-logs-path")).toHaveText(info.dir);
});

test("the application log shows the last hour, newest at the bottom", async ({ page }) => {
  const lines = Array.from({ length: 200 }, (_, i) => `2026-10-12 10:${String(i % 60).padStart(2, "0")} line ${i}`);
  const asked = [];
  await page.route("**/api/logs/application**", (route) => {
    asked.push(new URL(route.request().url()).search);
    return route.fulfill({ status: 200, contentType: "text/plain", body: lines.join("\n") });
  });
  await openSettings(page, "logs");
  await page.locator('.settings-subtab[data-sub="application"]').click();

  const view = page.locator('.settings-logtail-view[data-view="application"]');
  await expect(view).toHaveValue(/line 199$/);
  expect(asked).toEqual(["?minutes=60"]);
  const atBottom = await view.evaluate((el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 2);
  expect(atBottom).toBe(true);
});

test("a log that cannot be read says so with its status, and Refresh tries again", async ({ page }) => {
  let status = 500;
  await page.route("**/api/logs/backend**", (route) =>
    status === 200
      ? route.fulfill({ status: 200, contentType: "text/plain", body: "backend ok" })
      : route.fulfill({ status, body: "" }));
  await openSettings(page, "logs");
  await page.locator('.settings-subtab[data-sub="backend"]').click();
  const view = page.locator('.settings-logtail-view[data-view="backend"]');
  await expect(view).toHaveValue("Failed to load the log (status 500).");

  status = 200;
  await page.locator('.settings-logtail-refresh[data-view="backend"]').click();
  await expect(view).toHaveValue("backend ok");
});
