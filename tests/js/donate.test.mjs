// The Donate dialog's logic (donate.js, donateLinks.js): which ways are set,
// where the arrow goes with and without an {amount} template, and how the
// custom amount field is read. A way is set only when donateLinks.js holds a
// real https:// address; anything else, including the empty placeholder, is
// not set, and a way that is not set shows no control.
//
// Run:  node tests/js/donate.test.mjs

import { donateUrl, readAmount, takesAmount, AMOUNT_MIN, AMOUNT_MAX } from "../../static/js/donate.js";
import { DONATE_LINKS, DONATE_LINK_TEMPLATES, donateHref, isConfigured } from "../../static/js/donateLinks.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error(`FAIL  ${name}${detail ? `\n      ${detail}` : ""}`);
  }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const warn = console.warn;
console.warn = () => {};

// ── Placeholders are not links ──
for (const value of ["", "   ", undefined, null, 0, {}]) {
  check(`placeholder ${JSON.stringify(value)} is not a link`, donateHref(value) === null, String(donateHref(value)));
}

// ── Anything that is not https is not a link ──
for (const value of ["liberapay.com/someone", "http://example.org/give", "javascript:alert(1)", "ftp://example.org", "not a url"]) {
  check(`"${value}" is not a link`, donateHref(value) === null, String(donateHref(value)));
}

// ── A real https address is used as written (trimmed) ──
check("https link is live", donateHref("https://example.org/give") === "https://example.org/give");
check("surrounding spaces are trimmed", donateHref("  https://example.org/give?x=1  ") === "https://example.org/give?x=1");

// ── Which ways are set: the one check every Donate control follows ──
const none = { stripe: "", liberapay: "" };
const one = { stripe: "https://example.org/stripe", liberapay: "" };
const bad = { stripe: "http://example.org/stripe", liberapay: "liberapay.com/x" };
const both = { stripe: "https://example.org/stripe", liberapay: "https://example.org/lp" };
check("no link: neither way is set", !isConfigured("stripe", none) && !isConfigured("liberapay", none));
check("one link: only that way is set", isConfigured("stripe", one) && !isConfigured("liberapay", one));
check("links that are not https are not set", !isConfigured("stripe", bad) && !isConfigured("liberapay", bad));
check("both links: both ways are set", isConfigured("stripe", both) && isConfigured("liberapay", both));
check("an unknown way is never set", !isConfigured("paypal", both));

// ── Where the arrow goes ──
const links = { stripe: "https://example.org/stripe", liberapay: "" };
const tpl = { stripe: "https://example.org/stripe?a={amount}", liberapay: "https://example.org/lp/{amount}" };
check("a placeholder way has no link", donateUrl("liberapay", 5, links, tpl) === null, "a template alone must not make a way live");
check("no template: the plain link, amount or not", donateUrl("stripe", 15, links, {}) === "https://example.org/stripe");
check("template but no amount: the plain link", donateUrl("stripe", null, links, tpl) === "https://example.org/stripe");
check("template and amount: the amount goes in", donateUrl("stripe", 15, links, tpl) === "https://example.org/stripe?a=15", String(donateUrl("stripe", 15, links, tpl)));
check("a template without {amount} is ignored", donateUrl("stripe", 15, links, { stripe: "https://example.org/other" }) === "https://example.org/stripe");
check("a template that is not https is ignored", donateUrl("stripe", 15, links, { stripe: "http://example.org/?a={amount}" }) === "https://example.org/stripe");
check("an amount below the minimum never reaches a template", donateUrl("stripe", 1, links, tpl) === "https://example.org/stripe");
check("an unknown way has no link", donateUrl("paypal", 5, links, tpl) === null);

// ── Which ways take the amount picked here (#1037) ──
// Only these show presets, a custom amount and the arrow; any other set way
// has a tile that opens its page directly.
check("no template: the way takes no amount", !takesAmount("stripe", links, {}));
check("an empty template: no amount", !takesAmount("stripe", links, { stripe: "" }));
check("an https template with {amount}: the way takes it", takesAmount("stripe", links, tpl));
check("a template without {amount}: no amount", !takesAmount("stripe", links, { stripe: "https://example.org/other" }));
check("a template that is not https: no amount", !takesAmount("stripe", links, { stripe: "http://example.org/?a={amount}" }));
check("a template on a way whose link is not set: no amount", !takesAmount("liberapay", links, tpl));
check("an unknown way: no amount", !takesAmount("paypal", links, tpl));
check("what takesAmount says matches where the arrow goes", ["stripe", "liberapay"].every((way) =>
  takesAmount(way, both, tpl) === (donateUrl(way, 15, both, tpl) !== donateUrl(way, null, both, tpl))));

// ── The custom amount field ──
check("the minimum is 2", AMOUNT_MIN === 2);
check("typing 15", same(readAmount("15"), { value: "15", amount: 15, hint: null }));
check("typing 1 on the way to 15 is not clamped yet", same(readAmount("1"), { value: "1", amount: null, hint: null }));
check("committing 1 raises it to 2", same(readAmount("1", true), { value: "2", amount: 2, hint: "min" }));
check("committing 0 raises it to 2", same(readAmount("0", true), { value: "2", amount: 2, hint: "min" }));
check("letters are dropped, with a hint", same(readAmount("1a5"), { value: "15", amount: 15, hint: "digits" }));
check("a decimal point is dropped too", readAmount("7.50").value === "750" && readAmount("7.50").hint === "digits");
check("a minus sign is dropped", same(readAmount("-5"), { value: "5", amount: 5, hint: "digits" }));
check("only letters leaves the field empty", same(readAmount("abc"), { value: "", amount: null, hint: "digits" }));
check("empty is no amount and no hint", same(readAmount(""), { value: "", amount: null, hint: null }));
check("leading zeros go", readAmount("0020").value === "20");
check("above the maximum while typing is no amount", same(readAmount(String(AMOUNT_MAX + 1)), { value: String(AMOUNT_MAX + 1), amount: null, hint: "max" }));
check("above the maximum on commit is lowered to it", same(readAmount("99999", true), { value: String(AMOUNT_MAX), amount: AMOUNT_MAX, hint: "max" }));

// ── What ships: every link is either a placeholder or a real https link ──
check("the two ways are Stripe and Liberapay", same(Object.keys(DONATE_LINKS), ["stripe", "liberapay"]));
check("the templates name the same two ways", same(Object.keys(DONATE_LINK_TEMPLATES), ["stripe", "liberapay"]));
for (const [way, value] of Object.entries(DONATE_LINKS)) {
  check(`${way} is a string`, typeof value === "string");
  check(`${way} is empty or https`, value === "" || donateHref(value) !== null, value);
}
for (const [way, value] of Object.entries(DONATE_LINK_TEMPLATES)) {
  check(`${way} template is empty or an https address with {amount}`, value === "" || (value.includes("{amount}") && donateHref(value.replace("{amount}", "5")) !== null), value);
}
// The shipped links: Liberapay is live, Stripe and both templates stay empty until the maintainer sets them.
check("shipped Liberapay link", DONATE_LINKS.liberapay === "https://liberapay.com/StemDeck/donate", DONATE_LINKS.liberapay);
check("shipped Stripe link is still empty", DONATE_LINKS.stripe === "");
check("shipped templates are empty", DONATE_LINK_TEMPLATES.stripe === "" && DONATE_LINK_TEMPLATES.liberapay === "");
check("the links cannot be changed at runtime", Object.isFrozen(DONATE_LINKS) && Object.isFrozen(DONATE_LINK_TEMPLATES));

console.warn = warn;
console.log(`donate: ${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
