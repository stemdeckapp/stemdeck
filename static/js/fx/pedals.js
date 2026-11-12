// One builder per pedal type, and a chain of them in series. Shared by the
// live engines (chainHost.js) and the export print (fxPrint.js), so the two
// cannot drift apart.

import { createDelayPedal } from "./delayPedal.js";
import { createChorusPedal } from "./chorusPedal.js";
import { createPhaserPedal } from "./phaserPedal.js";
import { createDrivePedal } from "./drivePedal.js";
import { createReverbPedal } from "./reverbPedal.js";

const BUILDERS = {
  reverb: createReverbPedal,
  delay: createDelayPedal,
  chorus: createChorusPedal,
  phaser: createPhaserPedal,
  drive: createDrivePedal,
};

/** The node graph of one pedal, or null for a type that has no builder. */
export function createPedalNode(ctx, pedal, env) {
  const build = BUILDERS[pedal.type];
  return build ? build(ctx, pedal, env) : null;
}

/**
 * The pedals of a chain in series, in array order: input -> p1 -> ... ->
 * output. Returns {input, output, pedals: Map(iid -> node), latencyFrames,
 * dispose()}. An empty chain is a single straight connection.
 */
export function createSeries(ctx, desc, env) {
  const input = ctx.createGain();
  const output = ctx.createGain();
  const pedals = new Map();
  let prev = input;
  let latencyFrames = 0;
  for (const pedal of desc.pedals) {
    const node = createPedalNode(ctx, pedal, env);
    if (!node) continue;
    pedals.set(pedal.iid, node);
    prev.connect(node.input);
    prev = node.output;
    latencyFrames += node.latencyFrames || 0;
  }
  prev.connect(output);
  return {
    input,
    output,
    pedals,
    latencyFrames,
    update(next, nextEnv, immediate) {
      for (const pedal of next.pedals) pedals.get(pedal.iid)?.update(pedal, nextEnv, immediate);
    },
    dispose() {
      for (const node of pedals.values()) node.dispose();
      for (const n of [input, output]) { try { n.disconnect(); } catch { /* gone */ } }
    },
  };
}
