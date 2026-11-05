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
// Privacy: the page only ever holds these as plain links. Nothing is loaded
// from Stripe or Liberapay until a person clicks a link, and then it opens
// in their own browser.

export const DONATE_LINKS = Object.freeze({
  stripe: "",
  liberapay: "",
});

export const DONATE_LINK_TEMPLATES = Object.freeze({
  stripe: "",
  liberapay: "",
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
