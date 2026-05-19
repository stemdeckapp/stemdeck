// ─── Persistent store (tauri-plugin-store via custom commands) ───
//
// Falls back to localStorage when running outside Tauri (browser dev mode).
// The store is backed by ~/Library/Application Support/app.stemdeck.desktop/user-data.json
// on macOS — outside WebKit's reach, so WebView resets can never destroy user data.

export async function storeGet(key, fallback = null) {
  if (window.__TAURI__?.core?.invoke) {
    try {
      const val = await window.__TAURI__.core.invoke("store_get", { key });
      return val ?? fallback;
    } catch (e) { console.warn("[store] get failed for", key, e); return fallback; }
  }
  try {
    const raw = localStorage.getItem(key);
    return raw !== null ? JSON.parse(raw) : fallback;
  } catch { return fallback; }
}

export async function storeSet(key, value) {
  if (window.__TAURI__?.core?.invoke) {
    try {
      await window.__TAURI__.core.invoke("store_set", { key, value });
    } catch (e) { console.warn("[store] set failed for", key, e); }
    return;
  }
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { console.warn("[store] localStorage set failed", e); }
}

// Debounced variant — coalesces rapid writes (e.g. mixer slider moves) into
// a single store write ~300ms after the last call. Each call snapshots the
// value so no mutation races occur.
const _storePending = new Map();
export function storeSetDebounced(key, value, delayMs = 300) {
  if (_storePending.has(key)) clearTimeout(_storePending.get(key));
  const snapshot = JSON.parse(JSON.stringify(value));
  _storePending.set(key, setTimeout(() => {
    _storePending.delete(key);
    storeSet(key, snapshot);
  }, delayMs));
}

// Keys that hold critical user data and must be migrated from localStorage
// to the store on first launch after this feature ships.
const _MIGRATE_KEYS = [
  "stemdeck.folders",
  "stemdeck.deleted_jobs",
  "stemdeck:selected-stems",
];

// One-time bootstrap: copy localStorage → store for existing users.
// On fresh installs (no localStorage data) this is a no-op that just writes
// the migration flag so future upgrades can safely clear WebKit.
export async function runStoreMigrationIfNeeded() {
  if (!window.__TAURI__?.core?.invoke) return;
  try {
    // Check if any critical key is absent from the store but present in localStorage.
    const needs = await Promise.all(
      _MIGRATE_KEYS.map(async (k) => {
        const inStore = await window.__TAURI__.core.invoke("store_get", { key: k });
        return inStore === null && localStorage.getItem(k) !== null;
      })
    ).then((r) => r.some(Boolean));

    if (needs) {
      // Migrate fixed keys.
      for (const k of _MIGRATE_KEYS) {
        try {
          const raw = localStorage.getItem(k);
          if (raw !== null) {
            await window.__TAURI__.core.invoke("store_set", { key: k, value: JSON.parse(raw) });
          }
        } catch (e) { console.warn("[store] migration failed for key", k, e); }
      }
      // Migrate per-job mix keys (stemdeck:mix:<jobId>).
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k?.startsWith("stemdeck:mix:")) {
          try {
            const raw = localStorage.getItem(k);
            if (raw !== null) {
              await window.__TAURI__.core.invoke("store_set", { key: k, value: JSON.parse(raw) });
            }
          } catch (e) { console.warn("[store] migration failed for key", k, e); }
        }
      }
    }

    // Write the flag regardless of whether migration was needed. This covers
    // fresh installs (no localStorage data) so future upgrades clear WebKit.
    await window.__TAURI__.core.invoke("mark_store_migration_done").catch((e) =>
      console.warn("[store] mark_store_migration_done failed:", e)
    );
  } catch (e) { console.warn("[store] migration error:", e); }
}

export function fmtTime(s) {
  if (!isFinite(s) || s < 0) return "00:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m.toString().padStart(2, "0")}:${sec}`;
}

// Ruler tick: M:SS with no leading zero on the minutes digit ("0:30", "1:00", "12:30").
export function fmtTickLabel(s) {
  if (!isFinite(s) || s < 0) return "0:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

export const $ = (id) => document.getElementById(id);