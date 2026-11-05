// Where Streamer Mode keeps a song's scene: one record per song in the app's
// store (utils.js), `stemdeck:streamer:scene:<jobId>` (streamerModel.js). A
// song without a record starts from the default scene; nothing is written
// until the scene is changed, so opening Streamer on a song leaves no trace.

import { storeGet, storeSetDebounced, storeFlushPending } from "../utils.js";
import { sceneKey, parseScene, serializeScene, defaultScene } from "./streamerModel.js";

/** The scene saved for `jobId`, or the default scene. Never throws. */
export async function loadScene(jobId) {
  if (!jobId) return defaultScene();
  try {
    const raw = await storeGet(sceneKey(jobId), null);
    return raw ? parseScene(raw) : defaultScene();
  } catch (err) {
    console.warn("[streamer] reading the scene failed:", err);
    return defaultScene();
  }
}

/** Save `scene` as `jobId`'s (debounced: a run of changes is one write). */
export function saveScene(jobId, scene) {
  if (!jobId || !scene) return;
  storeSetDebounced(sceneKey(jobId), serializeScene(scene));
}

/** Write a pending save now (the song is about to change). */
export function flushScene(jobId) {
  if (!jobId) return Promise.resolve();
  return Promise.resolve(storeFlushPending(sceneKey(jobId))).catch((err) => {
    console.warn("[streamer] saving the scene failed:", err);
  });
}
