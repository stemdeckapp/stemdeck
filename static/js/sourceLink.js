// A translated sentence with one external link inside it: the "Source" lines
// of Settings > Models (modelsView.js for the built-in models, myModelsView.js
// for the ones a person added). linkedSentence() is pure, so
// tests/js/models-view.test.mjs runs it under node; linkedLine() draws it.

/** The address of a source link as its text: the host and path, no scheme
 *  and no trailing slash ("github.com/spotify/basic-pitch"). */
export function linkText(url) {
  return String(url ?? "").replace(/^https?:\/\//i, "").replace(/\/+$/, "");
}

/** Only an https address of plain characters is ever made a link. */
export function isHttpsLink(url) {
  return typeof url === "string" && /^https:\/\/[^\s"'<>\\]+$/i.test(url);
}

// Stands in for the link inside a translated sentence, so the sentence keeps
// its own word order and the link is cut in where it falls.
const LINK_MARK = "\u0000link\u0000";

/**
 * A sentence with a link inside it, as the parts to draw:
 *   { before, link: { text, url } | null, after }
 * `key` has a {link} placeholder; `vars` fills the others. Without a usable
 * url the sentence is `plainKey`'s, with no link.
 */
export function linkedSentence(key, plainKey, vars, url, t) {
  if (!isHttpsLink(url)) return { before: t(plainKey, vars), link: null, after: "" };
  const text = t(key, { ...vars, link: LINK_MARK });
  const at = text.indexOf(LINK_MARK);
  if (at < 0) return { before: text, link: null, after: "" };
  return { before: text.slice(0, at), link: { text: linkText(url), url }, after: text.slice(at + LINK_MARK.length) };
}

/**
 * A line from linkedSentence()'s parts. target="_blank" is all an external
 * link needs: main.js hands it to the desktop app's open_url, so it opens in
 * the system browser and never in the app window. The arrow says it leaves
 * the app; it is not read out. Every part is text, never HTML.
 */
export function linkedLine(className, parts) {
  const line = document.createElement("div");
  line.className = className;
  line.append(parts.before);
  if (parts.link) {
    const a = document.createElement("a");
    a.className = "models-source-link";
    a.href = parts.link.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const arrow = document.createElement("span");
    arrow.className = "models-source-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "↗";
    a.append(parts.link.text, " ", arrow);
    line.append(a, parts.after);
  }
  return line;
}
