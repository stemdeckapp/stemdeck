// A band's Discogs profile in the artist box, for bands Wikipedia has no
// article on (or leaves gaps in).
//
// The page never talks to Discogs: the user's token stays on the server,
// which finds the band and answers GET /api/jobs/{id}/artist-extra (see
// app/pipeline/discogs.py). With no token set, that answers 404 at once and
// nothing is sent anywhere. This module is the pure part: checking the
// answer's shape, turning it into the box's band shape, and filling the gaps
// a Wikidata band has. artistInfo.js does the asking and the drawing.

import { artistMatchesName } from "./artistLookup.js";

const TEXT_MAX = 2000;
const LIST_MAX = 60;
// The kinds of link the box draws an icon for (artistInfo.js LINK_ICONS).
export const DISCOGS_LINK_KINDS = ["website", "bandcamp", "instagram", "facebook", "youtube", "spotify", "appleMusic"];
const DISCOGS_PAGE = /^https:\/\/www\.discogs\.com\/(?:[a-z]{2}\/)?artist\/\d{1,12}[-\w%.()]*$/;

const text = (value, max = TEXT_MAX) => (typeof value === "string" ? value.trim().slice(0, max) : "");
const texts = (list, max = LIST_MAX) => (Array.isArray(list) ? list.map((v) => text(v, 300)).filter(Boolean).slice(0, max) : []);

function webUrl(value) {
  if (typeof value !== "string" || value.length > 2048 || /[\s\p{Cc}]/u.test(value)) return "";
  if (!/^https?:\/\/[^/\\]/i.test(value)) return "";
  try {
    const url = new URL(value);
    if ((url.protocol !== "https:" && url.protocol !== "http:") || url.username || url.password) return "";
    return url.href;
  } catch {
    return "";
  }
}

/**
 * The server's answer, checked field by field, or null when it is not a
 * Discogs artist at all. Anything of the wrong shape is dropped rather than
 * repaired: the box puts these in with textContent and setAttribute only, and
 * only http(s) ever becomes a link.
 */
export function discogsExtraFromJson(json) {
  if (!json || typeof json !== "object") return null;
  const id = Number.isInteger(json.id) && json.id > 0 ? json.id : 0;
  const name = text(json.name, 300);
  if (!id || !name) return null;
  const links = [];
  for (const link of Array.isArray(json.links) ? json.links : []) {
    const url = webUrl(link?.url);
    if (url && DISCOGS_LINK_KINDS.includes(link.kind) && !links.some((l) => l.kind === link.kind)) {
      links.push({ kind: link.kind, url });
    }
  }
  const releases = (Array.isArray(json.releases) ? json.releases : [])
    .map((r) => ({ year: /^\d{4}$/.test(r?.year || "") ? r.year : "", title: text(r?.title, 300) }))
    .filter((r) => r.title)
    .slice(0, LIST_MAX);
  return {
    id,
    name,
    realName: text(json.real_name, 300),
    profile: texts(json.profile, 12).map((p) => p.slice(0, TEXT_MAX)),
    members: { current: texts(json.members?.current), former: texts(json.members?.former) },
    groups: texts(json.groups),
    links,
    releases,
    url: DISCOGS_PAGE.test(json.url || "") ? json.url : `https://www.discogs.com/artist/${id}`,
  };
}

/**
 * A band known only to Discogs, in the shape the box draws a Wikidata band
 * in (artistLookup.js lookupArtist). It has no Wikidata id, so it cannot be
 * saved on the track; `discogs.only` tells the box to credit Discogs alone.
 */
export function discogsBand(extra) {
  return {
    id: "",
    name: extra.name,
    englishName: "",
    nativeName: "",
    nativeLang: "",
    names: [extra.name],
    description: "",
    realName: extra.realName,
    image: "",
    history: extra.profile,
    historyLang: "",
    historyVariant: "",
    members: { current: extra.members.current.map((name) => ({ name, native: "" })), former: extra.members.former.map((name) => ({ name, native: "" })) },
    groups: extra.groups,
    albums: extra.releases,
    // Neither studio albums nor albums: every release under the artist's name.
    albumsStudioOnly: null,
    links: extra.links,
    articleUrl: "",
    discogs: { url: extra.url, only: true, filled: [] },
  };
}

/**
 * Whether Discogs is worth asking about a band the box has from Wikidata:
 * there is none, or Wikipedia and Wikidata leave its history, its members or
 * its albums empty. A band with all three is never asked about.
 */
export function needsDiscogs(artist) {
  if (!artist) return true;
  const { current = [], former = [] } = artist.members || {};
  return !artist.history?.length || (!current.length && !former.length) || !artist.albums?.length;
}

/**
 * Whether a Wikidata band and a Discogs artist are one: by the Discogs id
 * the Wikidata item gives (P1953) when it gives one, else by name, which the
 * server already corroborated with the track's own song.
 */
export function sameArtist(artist, extra) {
  if (!artist || !extra) return false;
  if (artist.discogsId) return String(artist.discogsId) === String(extra.id);
  return artistMatchesName(artist, extra.name);
}

/**
 * The band to show, given what Wikidata found (`artist`, or null) and what
 * Discogs found (`extra`, or null). Wikipedia comes first: Discogs only
 * fills what it left empty (the history, the members, the releases, links
 * of a kind it has none of), and `discogs.filled` names each section it
 * filled, so the box can say so. No Wikidata band: the Discogs one. A
 * Wikidata band that is another artist than Discogs': left as it is.
 */
export function withDiscogs(artist, extra) {
  if (!extra) return artist;
  if (!artist) return discogsBand(extra);
  if (!sameArtist(artist, extra)) return artist;
  const merged = { ...artist };
  const filled = [];
  if (!artist.history?.length && extra.profile.length) {
    Object.assign(merged, { history: extra.profile, historyLang: "", historyVariant: "" });
    filled.push("history");
  }
  const { current = [], former = [] } = artist.members || {};
  if (!current.length && !former.length && (extra.members.current.length || extra.members.former.length)) {
    merged.members = discogsBand(extra).members;
    filled.push("members");
  }
  if (!artist.albums?.length && extra.releases.length) {
    Object.assign(merged, { albums: extra.releases, albumsStudioOnly: null });
    filled.push("albums");
  }
  const have = new Set((artist.links || []).map((l) => l.kind));
  const added = extra.links.filter((l) => !have.has(l.kind)).map((l) => ({ ...l, fromDiscogs: true }));
  if (added.length) {
    merged.links = [...(artist.links || []), ...added];
    filled.push("links");
  }
  if (!filled.length) return artist;
  merged.discogs = { url: extra.url, only: false, filled };
  return merged;
}
