// A file dropped on the desktop app on Linux arrives through the shell, and is
// treated exactly as one dropped anywhere else.
//
// WebKitGTK never gives the page a dropped file. The drag reports its types as
// text/uri-list and text/html, never "Files", so file-drop.spec.mjs's HTML5
// path does not see it, and at drop time there is no File to take even if it
// did. Left alone, WebKit navigated the window to the file or typed its URI
// into the URL box (#672). On Linux the shell takes the drop instead
// (desktop/src-tauri/src/dropin.rs) and the page asks it what arrived.
//
// What these cover is the page's half of that contract, against a stand-in for
// the shell that answers the same three commands the same way. The Rust half
// has its own tests in dropin.rs. No test here drives the real shell, so a
// fault that lives only in Rust will not show up in this file.
import { test, expect } from "@playwright/test";
import {
  seedLibrary,
  stubTauri,
  stubExportEndpoints,
  stubUpdateCheck,
} from "./helpers.mjs";

const MB = 1024 * 1024;

/**
 * Replaces the shell's three drop commands with a controllable stand-in.
 *
 * Registered after stubTauri, so it wraps the invoke that stub installed.
 * `window.__shell.emit(signal)` is a drag event reaching the shell; the page's
 * waiting next_drop_signal call is answered from a log by cursor, the way
 * DropInbox answers it.
 */
async function standInShell(page, { native = true } = {}) {
  await page.addInitScript((nativeDrop) => {
    const inner = window.__TAURI__.core.invoke;
    let seq = 0;
    const log = [];
    let waiting = [];
    const shell = {
      calls: [],
      reads: [],
      unreadable: new Set(),
      sizes: {},
      emit(signal) {
        seq += 1;
        log.push([seq, signal]);
        waiting = waiting.filter((tryAnswer) => !tryAnswer());
      },
    };
    window.__shell = shell;
    window.__TAURI__.core.invoke = (cmd, args) => {
      if (cmd === "native_file_drop" || cmd === "next_drop_signal" || cmd === "read_dropped_file") {
        shell.calls.push(cmd);
      }
      if (cmd === "native_file_drop") return Promise.resolve(nativeDrop);
      if (cmd === "next_drop_signal") {
        const after = args?.after ?? seq;
        return new Promise((resolve) => {
          const tryAnswer = () => {
            const hit = log.find(([s]) => s > after);
            if (!hit) return false;
            resolve({ seq: hit[0], signal: hit[1] });
            return true;
          };
          if (!tryAnswer()) waiting.push(tryAnswer);
        });
      }
      if (cmd === "read_dropped_file") {
        shell.reads.push(args.id);
        if (shell.unreadable.has(args.id)) {
          return Promise.reject("could not open the file: No such file or directory");
        }
        return Promise.resolve(new Uint8Array(shell.sizes[args.id] ?? 44).buffer);
      }
      return inner(cmd, args);
    };
  }, native);
}

async function openDesktop(page, opts) {
  await seedLibrary(page);
  await stubTauri(page);
  await standInShell(page, opts);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // The watcher starts with the rest of the import wiring; wait for it to be
  // listening before any drag reaches the shell.
  await expect
    .poll(() => page.evaluate(() => window.__shell.calls.includes("next_drop_signal")))
    .toBe(opts?.native === false ? false : true);
}

/** A drag from the file manager, entering and then dropping. */
const drop = (page, files) =>
  page.evaluate((f) => {
    for (const d of f) window.__shell.sizes[d.id] = d.size;
    window.__shell.emit({ kind: "enter" });
    window.__shell.emit({ kind: "drop", files: f });
  }, files);

const state = (page) =>
  page.evaluate(() => {
    const armed = document.getElementById("fileInput")?._files || [];
    const err = document.getElementById("urlDropError");
    return {
      pill: !document.getElementById("filePill").classList.contains("hidden"),
      name: document.getElementById("fileName")?.textContent || "",
      armed: armed.map((f) => ({ name: f.name, size: f.size })),
      error: err && !err.classList.contains("hidden") ? err.textContent : "",
      hovering: document.querySelector(".url-wrap").classList.contains("drag-over"),
      reads: [...window.__shell.reads],
    };
  });

test.describe("a file dropped on the Linux desktop app", () => {
  test("arms the import like a file from the picker", async ({ page }) => {
    await openDesktop(page);
    await drop(page, [{ id: 7, name: "Take 3.flac", size: 3 * MB }]);

    await expect.poll(() => state(page).then((s) => s.pill)).toBe(true);
    const s = await state(page);
    expect(s.name).toBe("Take 3.flac");
    // The staged File carries the bytes the shell read, not just a name.
    expect(s.armed).toEqual([{ name: "Take 3.flac", size: 3 * MB }]);
    expect(s.reads).toEqual([7]);
    expect(s.error).toBe("");
  });

  test("a file that is not audio is refused without being read", async ({ page }) => {
    await openDesktop(page);
    await drop(page, [{ id: 1, name: "notes.txt", size: 10 }]);

    await expect.poll(() => state(page).then((s) => s.error)).toContain("MP3");
    const s = await state(page);
    expect(s.pill).toBe(false);
    expect(s.reads).toEqual([]);
  });

  test("a file over the limit is refused without being read", async ({ page }) => {
    // Screening happens on the name and size the shell reports, so a file that
    // is going to be turned away is never loaded into memory to find that out.
    await openDesktop(page);
    await drop(page, [{ id: 1, name: "huge.wav", size: 900 * MB }]);

    await expect.poll(() => state(page).then((s) => s.error)).toContain("too large");
    expect((await state(page)).reads).toEqual([]);
  });

  test("a folder is refused as not audio", async ({ page }) => {
    // The shell reports a folder with a size of 0 and its own name.
    await openDesktop(page);
    await drop(page, [{ id: 1, name: "Stems", size: 0 }]);

    await expect.poll(() => state(page).then((s) => s.error)).toContain("MP3");
    expect((await state(page)).reads).toEqual([]);
  });

  test("of a mixed drop, only the audio is read", async ({ page }) => {
    await openDesktop(page);
    await drop(page, [
      { id: 1, name: "a.wav", size: 1000 },
      { id: 2, name: "cover.jpg", size: 1000 },
      { id: 3, name: "b.mp3", size: 1000 },
    ]);

    await expect.poll(() => state(page).then((s) => s.armed.length)).toBe(2);
    const s = await state(page);
    expect(s.armed.map((f) => f.name)).toEqual(["a.wav", "b.mp3"]);
    expect(s.reads).toEqual([1, 3]);
  });

  test("a file that has gone by the time it is read says so, by name", async ({ page }) => {
    await openDesktop(page);
    await page.evaluate(() => window.__shell.unreadable.add(5));
    await drop(page, [{ id: 5, name: "moved.wav", size: 1000 }]);

    await expect.poll(() => state(page).then((s) => s.error)).toContain("moved.wav");
    expect((await state(page)).pill).toBe(false);
  });

  test("the URL zone lights while a drag is over the window", async ({ page }) => {
    // The HTML5 hover cue never fires on Linux either, for the same reason the
    // drop did not: the drag never says it carries files.
    await openDesktop(page);

    await page.evaluate(() => window.__shell.emit({ kind: "enter" }));
    await expect.poll(() => state(page).then((s) => s.hovering)).toBe(true);

    await page.evaluate(() => window.__shell.emit({ kind: "leave" }));
    await expect.poll(() => state(page).then((s) => s.hovering)).toBe(false);
  });

  test("keeps listening after a drop", async ({ page }) => {
    await openDesktop(page);
    await drop(page, [{ id: 1, name: "first.wav", size: 1000 }]);
    await expect.poll(() => state(page).then((s) => s.name)).toBe("first.wav");

    await drop(page, [{ id: 2, name: "second.wav", size: 1000 }]);
    await expect.poll(() => state(page).then((s) => s.name)).toBe("second.wav");
  });

  test("where the shell does not take drops, the page does not wait on it", async ({ page }) => {
    // Windows and macOS: the HTML5 path carries the files there, and a watcher
    // parked on a command that will never answer would be a call for nothing.
    await openDesktop(page, { native: false });
    await page.waitForTimeout(500);

    const calls = await page.evaluate(() => window.__shell.calls);
    expect(calls).toEqual(["native_file_drop"]);
  });
});
