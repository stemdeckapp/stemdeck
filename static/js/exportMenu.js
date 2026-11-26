// One export menu: a button that opens a panel with a format choice (a
// radiogroup) and action rows. Performance's Export Performance menu
// (streamer/streamer.js) and the Studio's Export Mix (main.js) are both this.
//
// It owns what both have to get the same: the button's aria-expanded, the
// radiogroup (aria-checked and the state class, Left and Right between the
// formats), the keyboard (Down opens the menu and goes into its rows, Up and
// Down move between them, Escape closes it and gives the button its focus
// back), and rows that are disabled (aria-disabled="true") doing nothing,
// whether clicked or reached by key. What the rows are, what they do and when
// they are disabled stays with the caller.

const DISABLED = "true";

/** Whether a row or a format button is disabled. */
export const isDisabled = (el) => !el || el.getAttribute("aria-disabled") === DISABLED;

/**
 * @param {object} o
 * @param {HTMLElement} o.button       the button that opens and closes the menu
 * @param {HTMLElement} o.panel        the menu itself
 * @param {{value: string, el: HTMLElement}[]} [o.formats] the format buttons (role="radio")
 * @param {string} [o.initialFormat]
 * @param {string} [o.activeClass]     the chosen format's class ("active" in the Studio)
 * @param {(value: string) => boolean} [o.formatDisabled] a format that cannot be chosen now
 * @param {(value: string) => void} [o.onFormat] after the format changed
 * @param {() => HTMLElement[]} [o.rows] the action rows, in keyboard order (the shown ones)
 * @param {() => boolean} [o.canOpen]  false while the menu must stay shut (an export runs)
 * @param {() => void} [o.onOpen]      before the panel shows (re-check the gates here)
 * @param {() => void} [o.onClose]
 * @param {() => void} [o.closeOthers] before opening: close any other menu
 * @param {string|null} [o.hiddenClass] the class that hides the panel, or null for `hidden`
 */
export function createExportMenu({
  button, panel, formats = [], initialFormat, activeClass = "active", formatDisabled,
  onFormat, rows = () => [], canOpen = () => true, onOpen, onClose, closeOthers, hiddenClass = null,
}) {
  let format = initialFormat ?? formats[0]?.value ?? "";

  const isOpen = () => Boolean(panel) && (hiddenClass ? !panel.classList.contains(hiddenClass) : !panel.hidden);

  function show(on) {
    if (!panel) return;
    if (hiddenClass) panel.classList.toggle(hiddenClass, !on);
    else panel.hidden = !on;
    button?.setAttribute("aria-expanded", String(on));
  }

  function open() {
    if (isOpen() || !canOpen()) return;
    closeOthers?.();
    onOpen?.();
    show(true);
  }

  function close() {
    if (!isOpen()) return;
    show(false);
    onClose?.();
  }

  function paintFormat() {
    for (const f of formats) {
      const on = f.value === format;
      f.el?.classList.toggle(activeClass, on);
      f.el?.setAttribute("aria-checked", String(on));
      // One tab stop for the group: the chosen format.
      if (f.el && f.el.getAttribute("role") === "radio") f.el.tabIndex = on ? 0 : -1;
    }
  }

  /** Choose a format (`silent`: without calling onFormat). */
  function setFormat(value, { silent = false } = {}) {
    if (!formats.some((f) => f.value === value)) return;
    format = value;
    paintFormat();
    if (!silent) onFormat?.(value);
  }

  // A format refused now: disabled on its button, or by the caller's rule.
  const refused = (f) => f.el?.getAttribute("aria-disabled") === DISABLED || Boolean(formatDisabled?.(f.value));

  for (const f of formats) {
    f.el?.addEventListener("click", (e) => {
      e.stopPropagation();
      if (refused(f)) return;
      setFormat(f.value);
    });
    f.el?.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      e.stopPropagation();
      const usable = formats.filter((g) => !refused(g) && g.el?.offsetParent !== null);
      const at = usable.indexOf(f);
      const next = usable[(at + (e.key === "ArrowRight" ? 1 : usable.length - 1)) % usable.length];
      if (!next || next === f) return;
      setFormat(next.value);
      next.el.focus();
    });
  }
  paintFormat();

  const enabledRows = () => rows().filter((r) => r && !isDisabled(r));

  button?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (isOpen()) close();
    else open();
  });
  button?.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown") return;
    e.preventDefault();
    open();
    if (isOpen()) enabledRows()[0]?.focus();
  });
  panel?.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
      button?.focus();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const list = enabledRows();
    if (!list.length) return;
    e.preventDefault();
    const at = list.indexOf(document.activeElement);
    const step = e.key === "ArrowDown" ? 1 : -1;
    const next = at < 0 ? (step > 0 ? 0 : list.length - 1) : (at + step + list.length) % list.length;
    list[next].focus();
  });

  /**
   * Run `action` when `row` is pressed, unless it is disabled. `gate` runs
   * first and may disable it (a key changed while the menu was open).
   */
  function wireRow(row, action, { gate } = {}) {
    row?.addEventListener("click", (e) => {
      e.stopPropagation();
      gate?.();
      if (isDisabled(row)) return;
      action(e);
    });
  }

  return {
    open,
    close,
    isOpen,
    setFormat,
    wireRow,
    get format() { return format; },
  };
}
