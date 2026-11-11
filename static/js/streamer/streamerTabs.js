// Performance's two tabs over the timeline card: Timeline (the I play
// select, the zoom, the sections, the ruler, the Video lane and the take
// lanes) and Effects (Hear myself through StemDeck and the live effects of
// the input, streamerFx.js).
//
// Switching tabs only shows one panel and hides the other (the hidden
// attribute): nothing is built again or taken down. The input, the monitor
// route and its chain play on whichever tab shows, and the timeline keeps
// following a take that records while Effects shows.
//
// The tab chosen is remembered per browser (localStorage, a convenience
// only): Timeline when nothing is stored, the store is blocked or holds
// something else. The arithmetic here has no DOM, so it is tested in Node;
// buildTabs draws the tab list (WAI-ARIA tabs with automatic activation).

export const TAB_KEY = "stemdeck.streamer.tab";
export const TABS = Object.freeze(["timeline", "effects"]);
export const DEFAULT_TAB = "timeline";

/** A stored value made a tab: anything unknown is the default. */
export function tabOf(v) {
  return TABS.includes(v) ? v : DEFAULT_TAB;
}

/** The tab kept in `storage` (localStorage), the default when it cannot be read. */
export function readTab(storage) {
  try {
    return tabOf(storage?.getItem(TAB_KEY));
  } catch (err) {
    console.warn("[streamerTabs] reading the chosen tab failed:", err);
    return DEFAULT_TAB;
  }
}

/** Keep `tab` in `storage`; false when it could not be stored (the tab still changes). */
export function writeTab(storage, tab) {
  try {
    storage?.setItem(TAB_KEY, tabOf(tab));
    return true;
  } catch (err) {
    console.warn("[streamerTabs] saving the chosen tab failed:", err);
    return false;
  }
}

/**
 * The tab a key moves to from `current` (Left and Right wrap around, Home
 * the first, End the last), or null for a key the tab list does not take.
 */
export function tabForKey(current, key) {
  const i = Math.max(0, TABS.indexOf(current));
  const n = TABS.length;
  if (key === "ArrowRight") return TABS[(i + 1) % n];
  if (key === "ArrowLeft") return TABS[(i - 1 + n) % n];
  if (key === "Home") return TABS[0];
  if (key === "End") return TABS[n - 1];
  return null;
}

/** The centre of the timeline's view, in seconds, kept while it hides (null: nothing to keep). */
export function viewCentre(scrollLeft, view, pps) {
  if (!(pps > 0) || !(view > 1)) return null;
  return (Math.max(0, scrollLeft) + view / 2) / pps;
}

/**
 * Build the tab list. `tx(node, key, attr)` sets a text again on a language
 * change (streamer.js). `panels` maps each tab to its panel element (given
 * an id here when it has none). `onLeave(tab)` runs while the tab being
 * left still shows (to measure it), `onChange(tab)` once the new one shows.
 * Returns { list, tabs, select(tab, {focus}), current() }.
 */
export function buildTabs({ tx, panels, onChange, onLeave, initial = DEFAULT_TAB, labels }) {
  const list = document.createElement("div");
  list.className = "sm-tabs";
  list.setAttribute("role", "tablist");
  tx(list, "streamer.tabs.aria", "aria-label");
  const tabs = {};
  let current = tabOf(initial);
  for (const name of TABS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `sm-tab sm-tab-${name}`;
    b.id = `streamerTab-${name}`;
    b.dataset.tab = name;
    b.setAttribute("role", "tab");
    const text = document.createElement("span");
    text.className = "sm-tab-text";
    tx(text, labels[name]);
    b.append(text);
    const panel = panels[name];
    if (!panel.id) panel.id = `streamerPanel-${name}`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", b.id);
    b.setAttribute("aria-controls", panel.id);
    b.addEventListener("click", () => select(name, { focus: false }));
    b.addEventListener("keydown", (e) => {
      const next = tabForKey(current, e.key);
      if (!next) return;
      e.preventDefault();
      // The studio's arrows seek and its Home and End jump: not from here.
      e.stopPropagation();
      select(next, { focus: true });
    });
    tabs[name] = b;
    list.append(b);
  }
  function paint() {
    for (const name of TABS) {
      const on = name === current;
      tabs[name].setAttribute("aria-selected", String(on));
      tabs[name].tabIndex = on ? 0 : -1;
      panels[name].hidden = !on;
    }
  }
  function select(name, { focus = false } = {}) {
    const next = tabOf(name);
    const changed = next !== current;
    if (changed) onLeave?.(current);
    current = next;
    paint();
    if (focus) tabs[next].focus({ preventScroll: true });
    if (changed) onChange?.(next);
  }
  paint();
  return { list, tabs, select, current: () => current };
}
