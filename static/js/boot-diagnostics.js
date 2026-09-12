// Catch and show a failure that happens while the app is wiring itself up.
//
// main.js wires the UI in a long synchronous run of wireX() calls and only
// registers its own error handlers near the end of the module. A throw anywhere
// in that run therefore does two things at once: every listener after it never
// attaches, and the handlers that would have reported it never register either.
// What the user gets is an app that hovers and lets them type, because that is
// the engine's own behaviour, but whose buttons and file drop do nothing,
// because nothing is listening. Nothing is written anywhere (#618, found via
// #617).
//
// This module exists to be the thing that still works when that happens, so it
// follows three rules:
//
//   1. It is loaded BEFORE main.js in index.html. Module scripts execute in
//      document order, so its handlers are installed before main.js is
//      evaluated and are in place for anything main.js does, including a
//      failure to parse or to resolve an import.
//   2. It imports nothing. An import is a second thing that can fail, and it
//      would fail in exactly the circumstances this module needs to survive.
//   3. It styles itself inline. It cannot rely on a stylesheet having loaded,
//      and CSP allows inline style (`style-src 'self' 'unsafe-inline'`) while
//      forbidding inline script, which is also why this is a file rather than a
//      <script> block in the HTML.
//
// The strings here are deliberately English and do not go through i18n, which
// is the one place in the UI where that is correct: i18n.js is itself a module
// that can fail to load, and a banner that renders nothing when translation is
// broken defeats the entire point. An untranslated explanation beats a silent
// dead app.

(function () {
  const BANNER_ID = "boot-failure";
  let reported = false;

  function detailFrom(event) {
    if (event.type === "unhandledrejection") {
      const reason = event.reason;
      if (reason instanceof Error) return `${reason.message}\n${reason.stack || ""}`;
      return String(reason);
    }
    // A resource that failed to load (a module that 404s, say) fires an error
    // event on the element itself with no message, rather than a script error.
    const target = event.target;
    if (target && target !== window && (target.src || target.href)) {
      return `Failed to load: ${target.src || target.href}`;
    }
    const where = event.filename ? `\n${event.filename}:${event.lineno}:${event.colno}` : "";
    const stack = event.error && event.error.stack ? `\n${event.error.stack}` : "";
    return `${event.message || "Unknown error"}${where}${stack}`;
  }

  function show(detail) {
    // Only the first failure is shown. A broken wiring run usually produces
    // several, and the first is the one that caused the rest.
    if (reported) return;
    reported = true;

    const build = () => {
      if (document.getElementById(BANNER_ID)) return;

      const bar = document.createElement("div");
      bar.id = BANNER_ID;
      bar.setAttribute("role", "alert");
      bar.style.cssText = [
        "position:fixed", "inset:0 0 auto 0", "z-index:2147483647",
        "background:#3b1111", "color:#f6e9e9", "border-bottom:1px solid #d65a4a",
        "padding:14px 16px", "font:13px/1.5 system-ui,sans-serif",
        "max-height:50vh", "overflow:auto", "box-shadow:0 4px 24px rgba(0,0,0,0.4)",
      ].join(";");

      const title = document.createElement("strong");
      title.textContent = "StemDeck did not finish starting up.";
      title.style.cssText = "display:block;font-size:14px;margin-bottom:6px";

      const lead = document.createElement("div");
      lead.textContent =
        "Part of the interface never loaded, so buttons and file drop will not "
        + "respond. Hovering and typing still work because the browser handles "
        + "those itself. The cause is below. Please include it if you report this.";
      lead.style.cssText = "margin-bottom:10px;max-width:80ch";

      const pre = document.createElement("pre");
      pre.textContent = detail;
      // user-select is explicit because the app sets it to none in places, and
      // this text is the whole reason the banner exists.
      pre.style.cssText = [
        "margin:0 0 10px", "padding:8px 10px", "background:#2a0d0d",
        "border:1px solid #5c2222", "border-radius:6px", "white-space:pre-wrap",
        "word-break:break-word", "user-select:text", "-webkit-user-select:text",
        "font:12px/1.45 ui-monospace,Consolas,monospace",
      ].join(";");

      const hint = document.createElement("div");
      hint.textContent =
        "The full backend log is at /api/logs.zip, and the raw backend output at "
        + "/api/logs/backend. Both help identify whether a file failed to load.";
      hint.style.cssText = "opacity:0.85;margin-bottom:10px";

      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.textContent = "Dismiss";
      dismiss.style.cssText = [
        "background:#5c2222", "color:#f6e9e9", "border:1px solid #d65a4a",
        "border-radius:6px", "padding:5px 12px", "cursor:pointer", "font:inherit",
      ].join(";");
      dismiss.addEventListener("click", () => bar.remove());

      bar.append(title, lead, pre, hint, dismiss);
      document.body.prepend(bar);
    };

    if (document.body) build();
    else document.addEventListener("DOMContentLoaded", build, { once: true });
  }

  function handle(event) {
    const detail = detailFrom(event);
    // Still log it. In a browser the console is the faster route, and this runs
    // before main.js installs its own logging.
    console.error("[boot]", detail);
    show(detail);
  }

  // Capture phase, because a resource load failure fires on the element and
  // does not bubble. A bubbling-phase listener on window never sees a module
  // that 404s, which is one of the two causes this needs to tell apart.
  window.addEventListener("error", handle, true);
  window.addEventListener("unhandledrejection", handle);
})();
