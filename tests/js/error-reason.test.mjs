// The reason a failed request or a caught error gives (errorReason.js). The
// "Failed to restore track:" toast ended at the colon when either came back
// empty, so every shape that used to give "" is checked here.
//
// Run:  node tests/js/error-reason.test.mjs

import { errorReason, RESTART_REQUIRED, responseErrorMessage } from "../../static/js/errorReason.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const eq = (name, actual, expected) => check(name, actual === expected, `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);

const res = (status, statusText) => ({ status, statusText, ok: false });

// responseErrorMessage
eq("string detail wins", responseErrorMessage(res(503, "Service Unavailable"), { detail: "Queue is full" }), "Queue is full");
eq("blank detail falls to the status text", responseErrorMessage(res(422, "Unprocessable Entity"), { detail: "  " }), "Unprocessable Entity");
eq(
  "a list detail (FastAPI validation) gives its messages",
  responseErrorMessage(res(422, ""), { detail: [{ msg: "field required" }, { msg: "bad url" }] }),
  "field required; bad url",
);
// The old code: new Error([]) has the message "".
check("old code: an empty list detail gave an empty message", new Error([]).message === "");
eq("an empty list detail falls to the status text", responseErrorMessage(res(422, "Unprocessable Entity"), { detail: [] }), "Unprocessable Entity");
eq("no detail and no status text (HTTP/2) gives the code", responseErrorMessage(res(502, ""), {}), "HTTP 502");
eq("a body that was not JSON gives the code", responseErrorMessage(res(500, ""), null), "HTTP 500");
eq("an object detail is not shown as [object Object]", responseErrorMessage(res(400, ""), { detail: { code: 1 } }), "HTTP 400");
eq("no response at all is empty", responseErrorMessage(undefined, undefined), "");

// A write refused between a stems folder move and the restart: the sentence
// comes from the language tables, not the server's English detail.
const refused = { detail: "server words", code: "restart_required" };
eq("the code the server sends", RESTART_REQUIRED, "restart_required");
eq(
  "a refused write after a move gives the translated sentence",
  responseErrorMessage(res(409, "Conflict"), refused),
  TRANSLATIONS.en["settings.stemsLocation.restartToSave"],
);
eq("an unknown code still gives the detail", responseErrorMessage(res(409, ""), { detail: "busy", code: "other" }), "busy");

// errorReason
eq("an Error's message", errorReason(new Error("Failed to fetch")), "Failed to fetch");
eq("an Error with no message is empty", errorReason(new Error("")), "");
eq("a thrown string", errorReason("  offline "), "offline");
eq("a TypeError with no message is empty, not its name", errorReason(new TypeError("")), "");
eq("undefined is empty", errorReason(undefined), "");
eq("null is empty", errorReason(null), "");
eq("a number is empty", errorReason(42), "");

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
