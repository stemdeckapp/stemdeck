// Click-free AudioParam changes, shared by the EQ slot (fxChain.js) and the
// effects chain (fx/). Moved here unchanged from fxChain.js so the chain
// modules can use it without importing the engine glue.

export const PARAM_RAMP = 0.02; // seconds
export const XFADE = 0.02; // seconds

export function rampTo(param, value, ctx, immediate, time = PARAM_RAMP) {
  const now = ctx.currentTime;
  if (immediate) {
    param.cancelScheduledValues?.(0);
    param.value = value;
    return;
  }
  // Not cancelAndHoldAtTime: measured in Chromium, holding a ramp that is
  // still running and starting the next from it clicked on every knob step
  // (a 0.22 jump against 0.001 for the same sweep this way). The value the
  // param has now is where the next ramp starts.
  const current = param.value;
  param.cancelScheduledValues?.(now);
  param.setValueAtTime?.(current, now);
  if (typeof param.linearRampToValueAtTime === "function") {
    param.linearRampToValueAtTime(value, now + time);
  } else {
    param.value = value;
  }
}
