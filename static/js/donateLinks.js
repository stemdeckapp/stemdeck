// Where the Donate dialog's arrow button goes (donate.js). The only place these
// links live.
//
// DONATE_LINKS: one plain address per way to give.
//
//   stripe     a Stripe Payment Link (the "One-time donation" tile)
//   liberapay  the StemDeck page on liberapay.com (the "Recurring donation" tile)
//
// An empty string is a placeholder: while the selected way's link is empty the
// arrow is a disabled "Coming soon" and the dialog holds no link. Paste the full
// https:// address to make that way live; nothing else needs to change. Anything
// that is not an https:// address is treated as a placeholder too, so a
// half-pasted link never ships as a broken button.
//
// DONATE_LINK_TEMPLATES (optional): an address with {amount} in it, for a page
// that really does take the amount from its address. The amount a person picks
// (whole US dollars, 2 or more) replaces {amount}. Leave it empty and the arrow
// opens the plain link above, so the amount stays a choice in the dialog only.
// A template is used only when the plain link is also set and an amount is
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
// from Stripe or Liberapay until a person clicks the arrow, and then it opens
// in their own browser.

export const DONATE_LINKS = Object.freeze({
  stripe: "",
  liberapay: "",
});

export const DONATE_LINK_TEMPLATES = Object.freeze({
  stripe: "",
  liberapay: "",
});
