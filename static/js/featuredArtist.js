// The featured artist at the top of the We Recommend dialog (#1092): the
// list of featured artists, which entry is current, which came before it, and
// what the partner grid leaves out. Pure, with no DOM, so node and the browser
// tests import the list from here: changing the featured artist is a change
// to FEATURED_ARTISTS alone. catalog.js holds the partners (FRIEND_GROUPS) and
// draws the card.
//
// The last entry of the list is the current one. There is no date and no
// clock: the order of the list alone says who is current and who came before,
// and the card stays until the next entry is added.

/**
 * The featured artists, newest last. The last entry is the current one; the
 * ones before it are "Previously featured". Add an entry to feature someone
 * new; never remove one that was shown, or it drops out of that row.
 *
 * - `name` must be a member of FRIEND_GROUPS in catalog.js, which gives the
 *   photo and the role line. An entry that names nobody is skipped. The
 *   current artist is left out of the grid below the card; earlier ones stay.
 * - `quoteKey` (optional) is an i18n key holding the maintainer's own words.
 *   With no key, or a key no table has, the card draws no quote.
 * - `track` (optional) is { title, kindKey, year, url }: one track to start
 *   with, a single external link. `kindKey` is the release type ("Single").
 *   With no track, the card has no "Start here" row.
 * - `links` are the artist's pages, as { label, url }, opening externally.
 */
export const FEATURED_ARTISTS = [
  {
    id: "analog4lyfe",
    name: "Analog4Lyfe",
    quoteKey: "friends.featured.quote.analog4lyfe",
    links: [{ label: "Instagram", url: "https://www.instagram.com/analog4lyfe" }],
  },
];

/** The FRIEND_GROUPS member called `name`, or null. Names are proper nouns,
 *  identical in every language, so they match exactly. */
export function findMember(groups, name) {
  if (!name) return null;
  for (const group of groups || []) {
    for (const member of group.members || []) {
      if (member.name === name) return member;
    }
  }
  return null;
}

/**
 * The entries that name a member, in list order, each with that member. An
 * entry whose name matches nobody is left out: its photo and role come from
 * the member, so it has nothing to show.
 */
export function resolveFeatured(featured, groups) {
  const out = [];
  for (const entry of featured || []) {
    const member = findMember(groups, entry?.name);
    if (member) out.push({ entry, member });
  }
  return out;
}

/**
 * `current` is the last entry that names a member, or null when there is
 * none (the card is then not drawn). `previous` is everyone featured before
 * it, most recent first, once each, never the current artist.
 */
export function pickFeatured(featured, groups) {
  const resolved = resolveFeatured(featured, groups);
  const current = resolved.at(-1) ?? null;
  const previous = [];
  const seen = new Set(current ? [current.entry.name] : []);
  for (let i = resolved.length - 2; i >= 0; i--) {
    const { name } = resolved[i].entry;
    if (seen.has(name)) continue;
    seen.add(name);
    previous.push(resolved[i]);
  }
  return { current, previous };
}

/**
 * FRIEND_GROUPS without the current featured artist, so the card above the
 * grid is not repeated in it. Earlier featured artists stay. A group left
 * with no one is dropped, rather than drawn as a heading over nothing.
 */
export function groupsWithout(groups, name) {
  if (!name) return groups || [];
  return (groups || [])
    .map((group) => ({ ...group, members: (group.members || []).filter((m) => m.name !== name) }))
    .filter((group) => group.members.length > 0);
}

/**
 * How a member's image is drawn, as the grid does (friendCard in catalog.js):
 * "photo" for a square profile photo (`avatar: true`), cropped to fill;
 * "logo" for a wordmark, shown whole on a padded panel, since cropping would
 * cut the lettering; "none" when there is no image, which draws an initial.
 */
export function imageShape(member) {
  if (!member?.logo) return "none";
  return member.avatar ? "photo" : "logo";
}

/** The first letter of a name, for a slot with no image. */
export function initialOf(name) {
  return String(name ?? "").trim().charAt(0).toUpperCase() || "?";
}

/**
 * What the "Previously featured" row draws: one face per artist, in the order
 * pickFeatured gave them, with the image, how to draw it, and the letter to
 * fall back on.
 */
export function previousFaces(previous) {
  return (previous || []).map(({ entry, member }) => ({
    name: entry.name,
    image: member.logo || "",
    shape: imageShape(member),
    initial: initialOf(entry.name),
  }));
}

/** The grid card whose `data-friend` is `name`, or null. */
export function findFriendCard(cards, name) {
  for (const card of cards || []) if (card?.dataset?.friend === name) return card;
  return null;
}
