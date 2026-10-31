// Recording tracks as mixer lanes, DAW style.
//
// "Add track" (the button in the mixer header) creates an empty recording
// track: a mixer row and a waveform lane below the stems, named "Track 1",
// "Track 2" and so on. Each row carries:
//   - an arm button (record-enable). Record records into every armed track;
//   - an input selector, grouped as voice (a microphone: one input) and
//     instrument (an interface: "MG-XU · Input 2", "MG-XU · Input 1+2").
//     A device that is not connected shows as such, and Record refuses it.
//     Arming opens the input, and the armed track's meter shows what its own
//     channels receive (recordUi.js). Several tracks can be armed together only when they use
//     the same device: one stream, one clock, split by channel
//     (captureGraph.createRecorder). Arming a track on another device
//     disarms the rest and says why;
//   - a menu: rename the track, delete the track;
//   - a disclosure arrow: the track is a take folder, as in a DAW. Open, it
//     shows one lane per take under the track, newest on top, each with its
//     own waveform and a row here: play this take, rename, nudge, delete.
//     Clicking a take lane makes it the take that plays; the main row always
//     shows and plays that one. Open or closed is kept per track
//     (takeFolderModel.js has the rules).
//
// The lane is named by the track id (`k` plus 8 hex digits) and plays the
// track's active take. A take's audio.wav has the stems' exact format and
// length, so playback, mixdown, the zip and drag-out treat the lane as a
// stem; the server resolves a track id to its active take on export. Peaks
// are keyed by take id, so the lane borrows its active take's.
//
// After any change the server re-renders, and the studio reloads the job
// through a "stemdeck:takes-changed" event that catalog.js answers, the same
// way the vocal split brings in its new lanes.

import { isTakeName, setTakeLanes } from "../constants.js";
import { mixerEl, currentJobId, mixerState } from "../state.js";
import { applyMix, ensureMixerStateDefaults, renderMixerRow } from "../mixer.js";
import { t } from "../i18n.js";
import { showError } from "../job.js";
import { patchTake, deleteTake, createTrack, patchTrack, deleteTrack } from "./takeUpload.js";
import {
  FOLDER_STORE_KEY, parseFolderStore, withFolderChoice, isFolderOpen, openFolders, folderRowCount,
  LANE_ORDER_STEP, takeLaneOrder, nudgeText,
} from "./takeFolderModel.js";
import {
  defaultTrackInput, listInputDevices, resolveInput, sameDevice, fillInputSelect, choiceText, channelText, MISSING,
} from "./trackInputs.js";

export const NUDGE_MIN_MS = -2000;
export const NUDGE_MAX_MS = 2000;

let _jobId = null;
let _tracks = [];
// The add-track row (Audio track, Video track) in the mixer's lanes. Held
// here because a song load empties the mixer, and the row with its listeners
// goes back in on every sync.
let _addRow = null;
let _addBtns = [];
// Its place in the stack: right after the last extracted stem, before the
// tracks (player.js placeAddTrackRow sets it).
let _addOrder = "";

/** The CSS order of the add-track box, the same as its spacer in the waveform column. */
export function setAddTrackOrder(order) {
  _addOrder = String(order);
  if (_addRow) _addRow.style.order = _addOrder;
}

function setAddEnabled(on) {
  for (const b of _addBtns) b.disabled = !on;
}
const _armed = new Set();
// Tracks whose input the user picked in this session: never moved to the
// input that has signal (autoPickChannel), whatever it hears.
const _userPicked = new Set();
let _devices = [];
// Take folders need the SVG lane overlay (player.js, the Web Audio engine
// path). On the opt-out streaming path the lanes are WaveSurfer canvases fixed
// at load, with no room for a lane between two tracks, so there the folder
// stays shut and the track menu lists the takes instead.
let _foldersEnabled = false;
let _folderStore = null;
// Opened by hand in this page: keeps a one-take folder open (after deleting
// the second take, say) until the page reloads.
const _folderSession = {};

// ─── Model ───

/**
 * Lane entries for a job's tracks (the server's job state), in track order.
 * A track with no take yet is a lane with no audio: a row and an empty lane.
 */
export function takeStemsFromJob(takes, tracks) {
  if (!Array.isArray(tracks)) return [];
  const all = Array.isArray(takes) ? takes : [];
  return [...tracks]
    .filter((tr) => isTakeName(tr?.id))
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    .map((tr) => {
      const own = all.filter((tk) => tk?.track_id === tr.id);
      const active = own.find((tk) => tk.id === tr.active_take_id) || null;
      // The take and its nudge are in the URL, so switching takes or
      // re-rendering one is never answered from the browser's cache.
      const url = active?.url
        ? `${active.url}${active.url.includes("?") ? "&" : "?"}n=${active.user_nudge_ms || 0}`
        : null;
      return {
        name: tr.id,
        url,
        label: tr.name || tr.id,
        track: { ...tr, takes: own, active },
      };
    });
}

/** Peaks for track lanes, borrowed from each track's active take. */
export function withTrackPeaks(peaks) {
  if (!peaks || typeof peaks !== "object") return peaks;
  const out = { ...peaks };
  for (const tr of _tracks) {
    if (tr.active && peaks[tr.active.id]) out[tr.id] = peaks[tr.active.id];
  }
  return out;
}

export function getTracks() {
  return _tracks.slice();
}

export function armedTracks() {
  return _tracks.filter((tr) => _armed.has(tr.id));
}

/** The input a track records from (its own, or the Settings default). */
export function trackInput(track) {
  return resolveInput(track?.input, _devices);
}

/** Ask the studio to reload this job's lanes. */
export function requestTakesReload(jobId) {
  document.dispatchEvent(new CustomEvent("stemdeck:takes-changed", { detail: { jobId } }));
}

// ─── Arming ───

let _noteTimer = 0;

/** A short note under the mixer header, gone after a few seconds. */
export function showTrackNote(text) {
  const note = document.getElementById("trackNote");
  if (!note) return;
  note.textContent = text || "";
  note.hidden = !text;
  // Fixed to the window, under the mixer header: the header clips anything
  // hanging out of it.
  const head = note.parentElement?.getBoundingClientRect();
  if (head && text) {
    note.style.left = `${Math.round(head.left + 8)}px`;
    note.style.top = `${Math.round(head.bottom + 4)}px`;
    note.style.width = `${Math.max(160, Math.round(head.width - 16))}px`;
  }
  clearTimeout(_noteTimer);
  if (text) _noteTimer = setTimeout(() => { note.textContent = ""; note.hidden = true; }, 7000);
}

/**
 * Arm or disarm a track. Tracks armed together must share a device: the
 * others are disarmed (with a note) when this one is on a different device,
 * because separate devices run on separate clocks. A video track records no
 * sound, so it has no device to share: it arms beside any audio track (a
 * guitar on the interface and the camera, together).
 */
export function setArmed(trackId, on) {
  const track = _tracks.find((tr) => tr.id === trackId);
  if (!track) return;
  if (!on) {
    _armed.delete(trackId);
  } else if (track.kind === "video") {
    _armed.add(trackId);
  } else {
    const mine = trackInput(track);
    let dropped = false;
    for (const other of armedTracks()) {
      if (other.kind === "video") continue;
      if (other.id !== trackId && !sameDevice(trackInput(other), mine)) {
        _armed.delete(other.id);
        dropped = true;
      }
    }
    _armed.add(trackId);
    if (mine.missing) showTrackNote(t("rec.status.trackInputMissing", { name: track.name }));
    else if (dropped) showTrackNote(t("rec.track.otherDevice"));
  }
  renderArmButtons();
  document.dispatchEvent(new CustomEvent("stemdeck:tracks-armed"));
}

function renderArmButtons() {
  for (const btn of mixerEl?.querySelectorAll(".track-arm") || []) {
    const on = _armed.has(btn.dataset.track);
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.closest(".lane-header")?.classList.toggle("armed", on);
  }
}

// ─── Rows ───

/**
 * Register the job's tracks as lanes and build their mixer rows. Called at the
 * top of wireUpAudio, before the mix state loads.
 */
export function syncTakeLanes(jobId, stems) {
  if (jobId !== _jobId) {
    _armed.clear();
    for (const tr of _tracks) unmountLive(tr.id);
  }
  _jobId = jobId;
  _tracks = (stems || []).filter((s) => isTakeName(s?.name) && s.track).map((s) => ({
    ...s.track,
    name: s.track.name || s.name,
    url: s.url,
  }));
  for (const id of [..._armed]) if (!_tracks.some((tr) => tr.id === id)) _armed.delete(id);
  setTakeLanes(_tracks.map((tr) => ({ id: tr.id, label: tr.name })));
  ensureMixerStateDefaults();
  closeTakeMenu();
  setAddEnabled(!!jobId);
  if (!mixerEl) return;
  for (const row of mixerEl.querySelectorAll(".lane-header[data-take], .take-folder-row")) row.remove();
  for (const track of _tracks) mixerEl.appendChild(buildRow(track));
  if (_addRow) {
    _addRow.hidden = !jobId;
    _addRow.style.order = _addOrder;
    mixerEl.appendChild(_addRow);
  }
  renderFolderRows();
  renderArmButtons();
  refreshTrackInputs();
}

function buildRow(track) {
  const { row } = renderMixerRow({ name: track.id, url: track.url || "#" });
  row.dataset.take = track.id;
  row.classList.add("take-lane", "track-lane");
  row.classList.toggle("track-empty", !track.url);
  row.classList.toggle("track-video", track.kind === "video");
  // A video track is picture only: no input to choose, no level to show.
  const video = track.kind === "video";
  // Its M and S are hidden: a solo or mute left on it from before would
  // silence the song (or nothing) with no button to undo it.
  const st = mixerState[track.id];
  if (video && st && (st.soloed || st.muted)) {
    st.soloed = false;
    st.muted = false;
    applyMix();
  }
  // A recorded part is not transposed per lane, so the key stepper goes, and
  // so does the dB readout: the row's width goes to the fader and the picker.
  row.querySelector(".lane-key")?.remove();
  const dl = row.querySelector(".lane-dl");
  // Nothing to download from a track with no take. A class, not `hidden`:
  // the mixer's own rule for .lane-dl sets its display.
  if (dl && !track.url) dl.classList.add("track-dl-off");

  const arm = document.createElement("button");
  arm.type = "button";
  arm.className = "track-arm mx-btn";
  // The dot is drawn in CSS: hollow when idle, filled and glowing when armed.
  arm.dataset.track = track.id;
  arm.setAttribute("aria-pressed", "false");
  arm.setAttribute("aria-label", t("rec.track.armAria", { name: track.name }));
  arm.title = t("rec.track.armTitle");
  arm.addEventListener("click", () => setArmed(track.id, !_armed.has(track.id)));

  const input = document.createElement("select");
  input.className = "track-input";
  input.dataset.track = track.id;
  input.setAttribute("aria-label", t("rec.track.inputAria", { name: track.name }));
  input.addEventListener("change", () => onInputChange(track, input));

  const menuBtn = document.createElement("button");
  menuBtn.type = "button";
  menuBtn.className = "take-menu-btn mx-btn";
  menuBtn.setAttribute("aria-haspopup", "dialog");
  menuBtn.setAttribute("aria-expanded", "false");
  menuBtn.setAttribute("aria-label", t("rec.track.menuAria", { name: track.name }));
  menuBtn.title = t("rec.track.menu");
  menuBtn.textContent = "⋯";
  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openTrackMenu(menuBtn, track);
  });

  // The stem rows' side-rail card (mixer.js renderMixerRow), with a track's
  // own controls in it: the arm button and the input picker on the card's
  // bottom line, the track menu at the end of the name's line. The key column
  // stays, empty, so the fader and the rail line up with the stems'. The
  // wrapper draws the picker's chevron, so the select stays a native,
  // keyboard-friendly control.
  const picker = document.createElement("span");
  picker.className = "track-input-wrap";
  picker.append(input);
  row.querySelector(".mx-actions")?.append(...(video ? [arm] : [arm, picker]));
  row.querySelector(".mx-head")?.append(menuBtn);
  // The key column, empty on a track (no transpose): its tools instead.
  // Preview for a video track, and Delete for any track.
  const tools = document.createElement("div");
  tools.className = "mx-keyrail-tools";
  if (track.kind === "video") {
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "track-tool track-preview";
    preview.title = t("video.previewOpen");
    preview.setAttribute("aria-label", t("video.previewOpenAria", { name: track.name }));
    preview.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9.5v5l4.5-2.5z" fill="currentColor"/></svg>';
    preview.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        (await import("../video/videoPreview.js")).openPreview(track.id);
      } catch (err) {
        console.warn("[tracks] opening the preview failed:", err);
      }
    });
    tools.append(preview);
  }
  tools.append(deleteToolFor(track));
  row.querySelector(".mx-keyrail")?.append(tools);
  // A video track: a camera beside the name, so it reads as one at a glance.
  if (track.kind === "video") {
    const cam = document.createElement("span");
    cam.className = "track-kind-icon";
    cam.title = t("rec.track.videoBadge");
    cam.setAttribute("role", "img");
    cam.setAttribute("aria-label", t("rec.track.videoBadge"));
    cam.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-3v10l-6-3z"/></svg>';
    row.querySelector(".mx-swatch")?.after(cam);
  }
  row.insertBefore(buildFolderToggle(track), row.querySelector(".mx-icon"));
  return row;
}

// ─── Take folders ───

function loadFolderStore() {
  if (_folderStore) return _folderStore;
  let raw = null;
  try {
    raw = localStorage.getItem(FOLDER_STORE_KEY);
  } catch (e) {
    console.warn("[tracks] reading the take folder state failed:", e);
  }
  _folderStore = parseFolderStore(raw);
  return _folderStore;
}

function saveFolderChoice(trackId, open) {
  _folderStore = withFolderChoice(loadFolderStore(), trackId, open);
  _folderSession[trackId] = open;
  try {
    localStorage.setItem(FOLDER_STORE_KEY, JSON.stringify(_folderStore));
  } catch (e) {
    // Private windows and full storage: the choice holds for this page only.
    console.warn("[tracks] saving the take folder state failed:", e);
  }
}

function folderOpen(track) {
  if (!_foldersEnabled) return false;
  return isFolderOpen({
    trackId: track.id,
    takeCount: track.takes?.length || 0,
    store: loadFolderStore(),
    session: _folderSession,
  });
}

/** The open folders, in stack order: [{trackId, takes: [{take, playing}]}]. */
export function takeFolderLanes() {
  return _foldersEnabled ? openFolders(_tracks, folderOpen) : [];
}

/** How many take lanes the open folders add to the lane stack. */
export function takeFolderRowCount() {
  return folderRowCount(takeFolderLanes());
}

/**
 * player.js says whether the take lanes can be drawn (the SVG overlay is the
 * visible waveform). Off, every folder is shut and the track menu lists the
 * takes.
 */
export function setTakeFoldersEnabled(on) {
  if (_foldersEnabled === !!on) return;
  _foldersEnabled = !!on;
  renderFolderRows();
}

/** Open or close a track's folder, and ask the studio to lay the lanes out again. */
export function setFolderOpen(trackId, open) {
  const track = _tracks.find((tr) => tr.id === trackId);
  if (!track || !_foldersEnabled || !track.takes?.length) return;
  saveFolderChoice(trackId, !!open);
  renderFolderRows();
  document.dispatchEvent(new CustomEvent("stemdeck:take-folders-changed", { detail: { trackId, open: !!open } }));
}

function buildFolderToggle(track) {
  const btn = el("button", "take-folder-toggle");
  btn.type = "button";
  btn.dataset.track = track.id;
  btn.setAttribute("aria-expanded", "false");
  const arrow = el("span", "take-folder-arrow");
  arrow.setAttribute("aria-hidden", "true");
  const count = el("span", "take-folder-count", String(track.takes?.length || 0));
  btn.append(arrow, count);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setFolderOpen(track.id, btn.getAttribute("aria-expanded") !== "true");
  });
  return btn;
}

function renderFolderToggles() {
  for (const btn of mixerEl?.querySelectorAll(".take-folder-toggle") || []) {
    const track = _tracks.find((tr) => tr.id === btn.dataset.track);
    const n = track?.takes?.length || 0;
    const open = !!track && folderOpen(track);
    // Kept in the layout when it does nothing, so the track's icon and name
    // never move sideways between a track with takes and one without.
    btn.classList.toggle("off", !_foldersEnabled || !n);
    btn.disabled = !_foldersEnabled || !n;
    btn.setAttribute("aria-expanded", String(open));
    btn.setAttribute("aria-label", t(open ? "rec.folder.closeAria" : "rec.folder.openAria", { name: track?.name || "", count: n }));
    btn.title = t(open ? "rec.folder.close" : "rec.folder.open");
    const count = btn.querySelector(".take-folder-count");
    if (count) count.textContent = String(n);
  }
}

/** Build the mixer rows of every open folder, each right under its track. */
function renderFolderRows() {
  if (!mixerEl) return;
  for (const row of mixerEl.querySelectorAll(".take-folder-row")) row.remove();
  renderFolderToggles();
  for (const folder of takeFolderLanes()) {
    const parent = mixerEl.querySelector(`.lane-header[data-take="${folder.trackId}"]`);
    const track = _tracks.find((tr) => tr.id === folder.trackId);
    if (!parent || !track) continue;
    // Shown open in this page: it stays open if takes are deleted down to
    // one, rather than shutting under the pointer.
    if (_folderSession[track.id] === undefined) _folderSession[track.id] = true;
    let after = parent;
    for (const { take, playing } of folder.takes) {
      const row = buildTakeRow(track, take, playing);
      after.after(row);
      after = row;
    }
  }
  syncTakeFolderOrder();
}

/**
 * Give each take row its place in the mixer column: right under its track,
 * newest first. player.js orders the lanes in steps of LANE_ORDER_STEP and
 * calls this afterwards.
 */
export function syncTakeFolderOrder() {
  if (!mixerEl) return;
  const seen = new Map();
  for (const row of mixerEl.querySelectorAll(".take-folder-row")) {
    const id = row.dataset.folderOf;
    const parent = mixerEl.querySelector(`.lane-header[data-take="${id}"]`);
    const k = seen.get(id) || 0;
    seen.set(id, k + 1);
    const base = parent?.style.order === "" ? NaN : Number(parent?.style.order);
    row.style.order = Number.isFinite(base) ? String(takeLaneOrder(base / LANE_ORDER_STEP, k)) : "";
  }
}

function buildTakeRow(track, take, playing) {
  const label = take.label || take.id;
  const row = el("div", "lane-header mx-row take-folder-row");
  row.dataset.takeId = take.id;
  row.dataset.folderOf = track.id;
  row.classList.toggle("playing", playing);

  const play = el("button", "take-play mx-btn");
  play.type = "button";
  play.setAttribute("aria-pressed", String(playing));
  play.setAttribute("aria-label", t(playing ? "rec.folder.playingAria" : "rec.folder.playAria", { take: label, name: track.name }));
  play.title = t(playing ? "rec.folder.playing" : "rec.folder.play");
  play.addEventListener("click", (e) => {
    e.stopPropagation();
    selectTake(track.id, take.id);
  });

  const name = el("span", "mx-name take-folder-name", label);
  name.title = label;
  const meta = el("span", "take-folder-meta");
  const bits = [];
  if (playing) bits.push(t("rec.folder.playing"));
  const nudge = nudgeText(take.user_nudge_ms);
  if (nudge) bits.push(t("rec.folder.nudged", { ms: nudge }));
  meta.textContent = bits.join(" · ");
  const col = el("div", "lane-name-vu take-folder-text");
  col.append(name, meta);

  const menuBtn = el("button", "take-folder-menu-btn mx-btn", "⋯");
  menuBtn.type = "button";
  menuBtn.setAttribute("aria-haspopup", "dialog");
  menuBtn.setAttribute("aria-expanded", "false");
  menuBtn.setAttribute("aria-label", t("rec.folder.menuAria", { take: label }));
  menuBtn.title = t("rec.folder.menu");
  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openTakeMenu(menuBtn, take);
  });

  row.append(el("span", "take-folder-indent"), play, col, menuBtn);
  // The whole row is the take: a click anywhere but its menu plays it.
  row.addEventListener("click", () => selectTake(track.id, take.id));
  return row;
}

/** Make a take the one its track plays (and so the one the main row shows). */
export async function selectTake(trackId, takeId) {
  const track = _tracks.find((tr) => tr.id === trackId);
  const jobId = _jobId;
  if (!track || !jobId || track.active?.id === takeId) return;
  if (!track.takes?.some((tk) => tk.id === takeId)) return;
  const sel = `.take-folder-row[data-folder-of="${trackId}"], .take-folder-lane[data-folder-of="${trackId}"]`;
  for (const r of document.querySelectorAll(sel)) r.classList.toggle("choosing", r.dataset.takeId === takeId);
  try {
    await patchTrack(jobId, trackId, { active_take_id: takeId });
    requestTakesReload(jobId);
  } catch (e) {
    console.warn("[tracks] switching the take failed:", e);
    for (const r of document.querySelectorAll(sel)) r.classList.remove("choosing");
    showError(t("rec.take.failed"));
  }
}

/** A take's own menu: rename, nudge, delete. */
function openTakeMenu(btn, take) {
  if (_menuBtn === btn) { closeTakeMenu(); return; }
  closeTakeMenu();
  const jobId = _jobId;
  const label = take.label || take.id;
  const menu = el("div", "take-menu");
  menu.setAttribute("role", "dialog");
  menu.setAttribute("aria-label", t("rec.folder.menuAria", { take: label }));
  menu.dataset.takeId = take.id;

  const status = el("p", "take-menu-status");
  status.setAttribute("role", "status");
  const fail = (e) => {
    console.warn("[tracks] take update failed:", e);
    status.textContent = t("rec.take.failed");
  };
  const done = () => { closeTakeMenu(); requestTakesReload(jobId); };

  const nameLabel = el("label", "take-menu-field");
  nameLabel.append(el("span", null, t("rec.take.rename")));
  const nameInput = el("input", "take-name-input");
  nameInput.type = "text";
  nameInput.maxLength = 80;
  nameInput.value = label;
  nameLabel.append(nameInput);

  const { nudgeLabel, nudgeInput, hint } = nudgeFields(take);

  const actions = el("div", "take-menu-actions");
  const apply = el("button", "take-apply", t("rec.take.apply"));
  apply.type = "button";
  actions.append(apply);
  const dangers = el("div", "take-menu-actions");
  const delTake = el("button", "take-delete take-delete-take", t("rec.take.delete"));
  delTake.type = "button";
  dangers.append(delTake);
  menu.append(nameLabel, nudgeLabel, hint, actions, dangers, status);

  apply.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    const nudge = clampNudge(nudgeInput.value);
    const fields = {};
    if (name && name !== label) fields.label = name;
    if (nudge !== (take.user_nudge_ms || 0)) fields.user_nudge_ms = nudge;
    if (!Object.keys(fields).length) { closeTakeMenu(); return; }
    apply.disabled = true;
    status.textContent = t("rec.take.saving");
    try {
      await patchTake(jobId, take.id, fields);
      done();
    } catch (e) {
      fail(e);
      apply.disabled = false;
    }
  });
  twoPress(delTake, t("rec.take.deleteConfirm"), async () => {
    try {
      await deleteTake(jobId, take.id);
      done();
    } catch (e) { fail(e); }
  });
  for (const inp of [nameInput, nudgeInput]) {
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") apply.click(); });
  }
  showMenu(menu, btn);
  nameInput.focus();
}

/** The nudge field of a take: a number and -/+ steps. */
function nudgeFields(take) {
  const nudgeLabel = el("label", "take-menu-field");
  nudgeLabel.append(el("span", null, t("rec.take.nudge")));
  const nudgeRow = el("div", "take-nudge-row");
  const minus = el("button", "take-nudge-step", "−");
  minus.type = "button";
  minus.setAttribute("aria-label", t("rec.take.nudgeLater"));
  const nudgeInput = el("input", "take-nudge-input");
  nudgeInput.type = "number";
  nudgeInput.step = "1";
  nudgeInput.min = String(NUDGE_MIN_MS);
  nudgeInput.max = String(NUDGE_MAX_MS);
  nudgeInput.value = String(take?.user_nudge_ms || 0);
  const plus = el("button", "take-nudge-step", "+");
  plus.type = "button";
  plus.setAttribute("aria-label", t("rec.take.nudgeEarlier"));
  nudgeRow.append(minus, nudgeInput, plus);
  nudgeLabel.append(nudgeRow);
  for (const b of [minus, nudgeInput, plus]) b.disabled = !take;
  const hint = el("p", "take-menu-hint", t("rec.take.nudgeHint"));
  // A step of 1 ms, 10 with Shift. Positive = the take sounded late, so it
  // moves earlier: the same sign as every latency in the take pipeline.
  const step = (dir, e) => {
    const by = e?.shiftKey ? 10 : 1;
    nudgeInput.value = String(clampNudge(Number(nudgeInput.value || 0) + dir * by));
  };
  minus.addEventListener("click", (e) => step(-1, e));
  plus.addEventListener("click", (e) => step(1, e));
  return { nudgeLabel, nudgeInput, hint };
}

/** Place a menu next to the button that opened it, inside the window. */
function showMenu(menu, btn) {
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect();
  const w = menu.offsetWidth;
  const h = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(Math.round(r.right - w), window.innerWidth - w - 8))}px`;
  const below = r.bottom + 6;
  menu.style.top = `${below + h > window.innerHeight - 8 ? Math.max(8, Math.round(r.top - h - 6)) : Math.round(below)}px`;
  _menu = menu;
  _menuBtn = btn;
  btn.setAttribute("aria-expanded", "true");
}

// The live waveform (static/js/capture/liveWave.js) is optional: a build
// without it records the same, only without the drawing while it records.
function unmountLive(trackId) {
  import("./liveWave.js")
    .then((m) => m.unmountLiveWave?.(trackId))
    .catch(() => { /* this build has no live waveform module */ });
}

// ─── Inputs ───

let _choiceByValue = new Map();
let _refreshing = null;

/** Fill every track's input selector from the current device list. */
export function refreshTrackInputs() {
  // One enumeration at a time; a call while one runs gets its result.
  if (_refreshing) return _refreshing;
  _refreshing = (async () => {
    try {
      // Names are read (and, in the desktop app, unlocked) only when a
      // track's picker is on screen: a song with no track never opens the mic.
      _devices = await listInputDevices({ unlock: _tracks.length > 0 });
      _choiceByValue = new Map();
      for (const select of mixerEl?.querySelectorAll(".track-input") || []) {
        const track = _tracks.find((tr) => tr.id === select.dataset.track);
        if (track) fillTrackSelect(select, track);
      }
    } finally {
      _refreshing = null;
    }
  })();
  return _refreshing;
}

function fillTrackSelect(select, track) {
  const inp = trackInput(track);
  const map = fillInputSelect(select, _devices, {
    withChannels: true,
    missing: inp.missing ? (inp.device_label || "") : null,
  });
  for (const [k, v] of map) _choiceByValue.set(k, v);
  const want = `${inp.device_id}\u0000${inp.channels.join(",")}`;
  if (inp.missing) select.value = MISSING;
  else if (map.has(want)) select.value = want;
  else if (select.options.length) select.selectedIndex = 0;
  select.disabled = !_devices.length;
  select.classList.toggle("missing", !!inp.missing);
  const picked = map.get(select.value);
  select.title = picked
    ? `${t(picked.device.kind === "voice" ? "rec.input.voiceKind" : "rec.input.instrumentKind")}: ${choiceText(picked.device, picked.channels)}`
    : (inp.missing ? select.selectedOptions[0]?.textContent || "" : t("rec.track.noInput"));
}

/**
 * Move an armed track to the input of its device that has signal, when its
 * own input is silent (recordUi.js decides). Not for a track whose input the
 * user picked in this session. Says so in the track note. Resolves whether
 * it moved.
 */
export async function autoPickChannel(trackId, channel) {
  const track = _tracks.find((tr) => tr.id === trackId);
  if (!track || track.kind === "video" || _userPicked.has(trackId) || !track.input) return false;
  const input = { ...track.input, channels: [channel] };
  track.input = input;
  // Once: a later silence on the new input does not move it again.
  _userPicked.add(trackId);
  showTrackNote(t("rec.track.autoInput", { name: track.name, input: channelText([channel]) }));
  refreshTrackInputs();
  document.dispatchEvent(new CustomEvent("stemdeck:tracks-armed"));
  try {
    await patchTrack(_jobId, track.id, { input });
  } catch (e) {
    console.warn("[tracks] saving the input failed:", e);
    showError(t("rec.track.failed"));
  }
  return true;
}

async function onInputChange(track, select) {
  const picked = _choiceByValue.get(select.value);
  if (!picked) return;
  const input = { device_id: picked.device.deviceId, device_label: picked.device.name || null, channels: picked.channels };
  track.input = input;
  _userPicked.add(track.id);
  // Tracks armed together share one device; a move to another device
  // disarms the rest.
  if (_armed.has(track.id)) setArmed(track.id, true);
  try {
    await patchTrack(_jobId, track.id, { input });
  } catch (e) {
    console.warn("[tracks] saving the input failed:", e);
    showError(t("rec.track.failed"));
  }
  refreshTrackInputs();
  document.dispatchEvent(new CustomEvent("stemdeck:tracks-armed"));
}

/**
 * Create an empty track on the default input and show it. `kind` "video"
 * makes a video track: Record films its takes with the camera as well.
 */
export async function addTrack(kind = "audio") {
  const jobId = _jobId || currentJobId;
  if (!jobId) return null;
  setAddEnabled(false);
  try {
    const input = await defaultTrackInput(kind);
    const fields = input ? { input } : {};
    if (kind === "video") fields.kind = "video";
    const track = await createTrack(jobId, fields);
    requestTakesReload(jobId);
    return track;
  } catch (e) {
    console.warn("[tracks] adding a track failed:", e);
    showError(t(e?.status === 409 ? "rec.track.tooMany" : "rec.track.failed"));
    return null;
  } finally {
    setAddEnabled(!!_jobId);
  }
}

export function initTrackControls() {
  _addRow = document.getElementById("addTrackRow");
  const audio = document.getElementById("addTrackBtn");
  const video = document.getElementById("addVideoTrackBtn");
  _addBtns = [audio, video].filter(Boolean);
  audio?.addEventListener("click", (e) => {
    e.stopPropagation();
    addTrack("audio");
  });
  video?.addEventListener("click", (e) => {
    e.stopPropagation();
    addTrack("video");
  });
  navigator.mediaDevices?.addEventListener?.("devicechange", () => { refreshTrackInputs(); });
}

// ─── Track menu ───

let _menu = null;
let _menuBtn = null;

function closeTakeMenu() {
  if (_menu) {
    _menu.remove();
    _menu = null;
  }
  _menuBtn?.setAttribute("aria-expanded", "false");
  _menuBtn = null;
}

document.addEventListener("click", (e) => {
  if (_menu && !_menu.contains(e.target)) closeTakeMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && _menu) {
    const btn = _menuBtn;
    closeTakeMenu();
    btn?.focus();
  }
});

export function clampNudge(ms) {
  const n = Math.round(Number(ms));
  if (!Number.isFinite(n)) return 0;
  return Math.max(NUDGE_MIN_MS, Math.min(NUDGE_MAX_MS, n));
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** Delete on the second press: a take is a performance, and there is no undo. */
// How long a pressed Delete waits for the second press.
const DELETE_ARM_MS = 3000;

/**
 * The track's Delete tool in its key column: one press arms it (it turns red
 * and says to press again), a second press within DELETE_ARM_MS deletes the
 * track and its takes, as Delete track in the track menu does.
 */
function deleteToolFor(track) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "track-tool track-delete";
  const idle = () => {
    btn.classList.remove("armed");
    btn.title = t("rec.track.deleteTool");
    btn.setAttribute("aria-label", t("rec.track.deleteToolAria", { name: track.name }));
  };
  idle();
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>';
  let timer = 0;
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!btn.classList.contains("armed")) {
      btn.classList.add("armed");
      btn.title = t("rec.track.deleteAgain", { name: track.name });
      btn.setAttribute("aria-label", t("rec.track.deleteAgain", { name: track.name }));
      timer = setTimeout(idle, DELETE_ARM_MS);
      return;
    }
    clearTimeout(timer);
    btn.disabled = true;
    const jobId = _jobId;
    try {
      _armed.delete(track.id);
      await deleteTrack(jobId, track.id);
      unmountLive(track.id);
      requestTakesReload(jobId);
    } catch (err) {
      console.warn("[tracks] deleting the track failed:", err);
      showError(t("rec.track.failed"));
      btn.disabled = false;
      idle();
    }
  });
  return btn;
}

function twoPress(btn, confirmText, action) {
  let armed = false;
  btn.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      btn.textContent = confirmText;
      btn.classList.add("armed");
      return;
    }
    await action();
  });
}

/**
 * The track's own menu: rename the track, delete it. Everything about a
 * single take lives on that take's row in the open folder. On the streaming
 * path, where folders cannot open, the menu also lists the takes (which one
 * plays) and nudges or deletes the playing one, as before folders existed.
 */
function openTrackMenu(btn, track) {
  if (_menuBtn === btn) { closeTakeMenu(); return; }
  closeTakeMenu();
  const jobId = _jobId;
  const active = track.active || null;
  const withTakes = !_foldersEnabled;
  const menu = el("div", "take-menu");
  menu.setAttribute("role", "dialog");
  menu.setAttribute("aria-label", t("rec.track.menuAria", { name: track.name }));
  menu.dataset.take = track.id;

  const status = el("p", "take-menu-status");
  status.setAttribute("role", "status");
  const fail = (e) => {
    console.warn("[tracks] update failed:", e);
    status.textContent = t("rec.take.failed");
  };
  const done = () => { closeTakeMenu(); requestTakesReload(jobId); };

  // Takes: which one plays (streaming path only).
  const takesBox = el("fieldset", "take-menu-takes");
  takesBox.append(el("legend", null, t("rec.track.takes")));
  if (!track.takes?.length) {
    takesBox.append(el("p", "take-menu-hint", t("rec.track.noTakes")));
  }
  for (const tk of track.takes || []) {
    const lab = el("label", "take-menu-take");
    const radio = el("input");
    radio.type = "radio";
    radio.name = `takes-${track.id}`;
    radio.value = tk.id;
    radio.checked = tk.id === active?.id;
    radio.addEventListener("change", async () => {
      status.textContent = t("rec.take.saving");
      try {
        await patchTrack(jobId, track.id, { active_take_id: tk.id });
        done();
      } catch (e) { fail(e); }
    });
    lab.append(radio, el("span", null, tk.label || tk.id));
    takesBox.append(lab);
  }

  // Name
  const nameLabel = el("label", "take-menu-field");
  nameLabel.append(el("span", null, t("rec.track.rename")));
  const nameInput = el("input", "take-name-input");
  nameInput.type = "text";
  nameInput.maxLength = 80;
  nameInput.value = track.name;
  nameLabel.append(nameInput);

  // Nudge, for the take that plays (streaming path only).
  const { nudgeLabel, nudgeInput, hint } = nudgeFields(active);

  const actions = el("div", "take-menu-actions");
  const apply = el("button", "take-apply", t("rec.take.apply"));
  apply.type = "button";
  actions.append(apply);
  const dangers = el("div", "take-menu-actions");
  const delTake = el("button", "take-delete take-delete-take", t("rec.take.delete"));
  delTake.type = "button";
  delTake.hidden = !active;
  const delTrack = el("button", "take-delete take-delete-track", t("rec.track.delete"));
  delTrack.type = "button";
  if (withTakes) dangers.append(delTake);
  dangers.append(delTrack);

  if (withTakes) menu.append(takesBox, nameLabel, nudgeLabel, hint, actions, dangers, status);
  else menu.append(nameLabel, actions, dangers, status);

  apply.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    const nudge = clampNudge(nudgeInput.value);
    const renamed = name && name !== track.name;
    const nudged = withTakes && active && nudge !== (active.user_nudge_ms || 0);
    if (!renamed && !nudged) { closeTakeMenu(); return; }
    apply.disabled = true;
    status.textContent = t("rec.take.saving");
    try {
      if (renamed) await patchTrack(jobId, track.id, { name });
      if (nudged) await patchTake(jobId, active.id, { user_nudge_ms: nudge });
      done();
    } catch (e) {
      fail(e);
      apply.disabled = false;
    }
  });

  twoPress(delTake, t("rec.take.deleteConfirm"), async () => {
    try {
      await deleteTake(jobId, active.id);
      done();
    } catch (e) { fail(e); }
  });
  twoPress(delTrack, t("rec.track.deleteConfirm"), async () => {
    try {
      _armed.delete(track.id);
      await deleteTrack(jobId, track.id);
      unmountLive(track.id);
      done();
    } catch (e) { fail(e); }
  });

  nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") apply.click(); });
  nudgeInput.addEventListener("keydown", (e) => { if (e.key === "Enter") apply.click(); });

  showMenu(menu, btn);
  nameInput.focus();
}
