// Where the Donate dialog's links go (donate.js). The only place these links
// live, and the only thing that decides whether any Donate control shows.
//
// DONATE_LINKS: one plain address per way to give. With no template below,
// that way's tile is itself the link: a press opens this page directly, and
// the dialog offers no amount for it.
//
//   stripe     a Stripe Payment Link (the "One-time donation" tile)
//   liberapay  the StemDeck page on liberapay.com (the "Recurring donation" tile)
//
// An empty string is a placeholder: a way whose link is not set has no tile in
// the dialog, and while neither is set the Donate button in the rail stays
// hidden. Paste the full https:// address to make that way appear; nothing else
// needs to change. Anything that is not an https:// address counts as not set
// too, so a half-pasted link never ships as a broken button.
//
// DONATE_LINK_TEMPLATES (optional): an address with {amount} in it, for a page
// that really does take the amount from its address. Only a way with one is a
// choice in the dialog, with the preset amounts, the custom amount and the
// arrow; the amount a person picks (whole US dollars, 2 or more) replaces
// {amount}. Leave it empty and the tile opens the plain link above directly,
// where the donor chooses the amount. A template is used only when the plain
// link is also set, and the arrow opens the plain link until an amount is
// picked. Do not guess a parameter name: a plain Stripe Payment Link and a
// Liberapay profile do not read an amount from their address in general, so
// only fill this from the platform's own documentation for the exact link in
// use. Example shape (not a real parameter):
//
//   stripe: "https://example.org/give?amount={amount}"
//
// Do not fill any of these with a guess: a made-up account name may belong to
// someone else.
//
// DONATE_QR (optional): a way set to true shows a small QR code inside its
// link tile, which opens the same page from a phone. Both ways show one now.
// It applies only to a way whose tile is a plain link (no template), and the
// code is drawn by this app's own server (/api/qr), only once the Donate dialog
// opens. Set a way to false and its tile is just the words and the mark.
//
// SOCIAL_LINKS: the project's own pages, shown as two small follow buttons in
// the Share card. The same rules as the links above: only a full https://
// address counts, an empty or other value hides that button, and nothing is
// loaded from Instagram or X until a person clicks. A click opens the person's
// own browser through the a[target="_blank"] handler in main.js, never inside
// the app.
//
// Privacy: the page only ever holds these as plain links. Nothing is loaded
// from Stripe or Liberapay until a person clicks a link, and then it opens
// in their own browser.

export const DONATE_LINKS = Object.freeze({
  stripe: "https://donate.stripe.com/4gM4gB3L5d325xebEG8g000",
  liberapay: "https://liberapay.com/StemDeck/donate",
});

export const DONATE_LINK_TEMPLATES = Object.freeze({
  stripe: "",
  liberapay: "",
});

export const DONATE_QR = Object.freeze({
  stripe: true,
  liberapay: true,
});

export const SOCIAL_LINKS = Object.freeze({
  instagram: "https://www.instagram.com/stemdeck/",
  x: "https://x.com/StemDeckApp",
});

/** The address a link opens, or null while it is a placeholder. Only https. */
export function donateHref(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  let url;
  try {
    url = new URL(value.trim());
  } catch (err) {
    console.warn("[donate] not a link, its way stays hidden:", value, err);
    return null;
  }
  return url.protocol === "https:" ? url.href : null;
}

/** Whether this way's link is set, so its Donate controls show. */
export function isConfigured(way, links = DONATE_LINKS) {
  return donateHref(links[way]) !== null;
}

/** The address a follow button opens, or null while it is not set. Only https. */
export function socialHref(way, links = SOCIAL_LINKS) {
  return donateHref(links?.[way]);
}
