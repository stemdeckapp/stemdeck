// Online sources (#1089): whether the page offers search and link import.
//
// The server owns the setting (`online_sources` in /api/settings) and refuses
// every request that would start yt-dlp while it is off, with 403 and the code
// "online_sources_off" (app/core/online_sources.py). This module keeps the
// page's copy of that answer, so the search box, the composer and the phone
// page do not offer something the server would refuse. Off until the server
// says otherwise: a page that cannot read the setting must not offer it either.
//
// The pure parts (reading the answer, telling a link from a search) are pinned
// by tests/js/online-sources.test.mjs without a browser.

import { ONLINE_SOURCES_OFF } from "./errorReason.js";

/** Whether a /api/settings answer has online sources on. Only a true counts. */
export function onlineSourcesFrom(settings) {
  return settings?.online_sources === true;
}

/** Whether a refused request's JSON body is the online sources refusal. */
export function isOnlineSourcesRefusal(data) {
  return data?.code === ONLINE_SOURCES_OFF;
}

// Anything that could be a link: it belongs to the import flow, not to search.
// Deliberately loose, the same test search.js has always used, so the two
// agree on which text is a link.
export const LOOKS_LIKE_URL = /^\s*(https?:\/\/|www\.)|\b(youtube\.com|youtu\.be|soundcloud\.com)\b/i;

/** Whether `text` is a link the import would fetch, rather than a search. */
export function looksLikeLink(text) {
  return LOOKS_LIKE_URL.test(String(text ?? ""));
}

let on = false;
const listeners = new Set();
// The first read of the setting from the server, once one has started.
let firstRead = null;

/** The page's copy of the setting. */
export function onlineSourcesOn() {
  return on;
}

/** Record the setting, and tell every listener when it changed. */
export function setOnlineSourcesState(value) {
  const next = value === true;
  if (next === on) return;
  on = next;
  for (const fn of listeners) {
    try {
      fn(on);
    } catch (e) {
      console.warn("[online sources] listener failed:", e);
    }
  }
}

/** Call `fn(on)` on every change. Returns the function that stops it. */
export function onOnlineSourcesChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Read the setting from the server. Leaves it as it was when that fails. */
export function refreshOnlineSources() {
  const read = (async () => {
    try {
      const r = await fetch("/api/settings", { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      setOnlineSourcesState(onlineSourcesFrom(await r.json()));
    } catch (e) {
      console.warn("[online sources] could not read the setting:", e);
    }
    return on;
  })();
  firstRead ??= read;
  return read;
}

/**
 * The setting once the page has asked the server at least once: the first
 * read if one is under way or done, else a read started now. Before a
 * refusal that the page decides on its own, so a click that comes before the
 * first answer is not refused for a setting that is on.
 */
export async function onlineSourcesKnown() {
  await (firstRead ?? refreshOnlineSources());
  return on;
}
