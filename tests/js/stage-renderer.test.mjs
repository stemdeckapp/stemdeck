// Performance Mode's stage renderer (static/js/streamer/stageRenderer.js), on
// a stub 2D context that records what is drawn: the camera covers the whole
// output at every aspect, nothing is drawn over it, and the output never
// carries the live window's guides.
import { renderStage, cameraCrop } from "../../static/js/streamer/stageRenderer.js";
import { outputSize } from "../../static/js/streamer/streamerModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const near = (a, b, e = 1e-6) => Math.abs(a - b) < e;

function stubContext() {
  const calls = [];
  const gradient = () => ({ addColorStop() {} });
  const rec = (name) => (...args) => { calls.push({ name, args }); };
  return {
    calls,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    font: "",
    textAlign: "",
    textBaseline: "",
    save: rec("save"),
    restore: rec("restore"),
    beginPath: rec("beginPath"),
    closePath: rec("closePath"),
    moveTo: rec("moveTo"),
    arcTo: rec("arcTo"),
    arc: rec("arc"),
    stroke: rec("stroke"),
    fillRect: rec("fillRect"),
    strokeRect: rec("strokeRect"),
    drawImage: rec("drawImage"),
    setLineDash: rec("setLineDash"),
    fillText: (text, x, y) => calls.push({ name: "fillText", args: [text, x, y] }),
    createLinearGradient: gradient,
  };
}

// A camera is anything drawImage takes with a video size.
const camera = (w, h) => ({ videoWidth: w, videoHeight: h });
const labels = { camera: "Camera", actionSafe: "Action safe", titleSafe: "Title safe" };
const draw = (cam, { width = 1920, height = 1080, editor = null } = {}) => {
  const ctx = stubContext();
  renderStage(ctx, cam, { width, height, font: "Inter", labels, editor });
  return ctx;
};
const of = (ctx, name) => ctx.calls.filter((c) => c.name === name);
const texts = (ctx) => of(ctx, "fillText").map((c) => c.args[0]);

// The output: the camera, full frame, and nothing else.
const cam = camera(1280, 720);
const out = draw(cam);
check("the output draws the camera once", of(out, "drawImage").length === 1 && of(out, "drawImage")[0].args[0] === cam);
check("the camera fills the whole 16:9 output", (() => {
  const [, sx, sy, sw, sh, dx, dy, dw, dh] = of(out, "drawImage")[0].args;
  return sx === 0 && sy === 0 && sw === 1280 && sh === 720 && dx === 0 && dy === 0 && dw === 1920 && dh === 1080;
})());
check("nothing is drawn over the camera: no text, no shapes, no guides", texts(out).length === 0 && of(out, "strokeRect").length === 0 && of(out, "setLineDash").length === 0 && of(out, "stroke").length === 0);
check("only the black backdrop is filled", of(out, "fillRect").length === 1 && near(of(out, "fillRect")[0].args[2], 1920));
check("save and restore balance", of(out, "save").length === of(out, "restore").length);

// Every aspect: covered, cut and centred, never stretched.
for (const aspect of ["16:9", "9:16", "1:1"]) {
  for (const short of [720, 1080]) {
    const { width, height } = outputSize(aspect, short);
    const c = draw(camera(1920, 1080), { width, height });
    const [, sx, sy, sw, sh, dx, dy, dw, dh] = of(c, "drawImage")[0].args;
    const inside = sx >= -1e-6 && sy >= -1e-6 && sx + sw <= 1920 + 1e-6 && sy + sh <= 1080 + 1e-6;
    const sameShape = near(sw / sh, width / height);
    const centred = near(sx, (1920 - sw) / 2) && near(sy, (1080 - sh) / 2);
    check(`${aspect} at ${short}p: the camera covers the output, cut and centred, not stretched`, dx === 0 && dy === 0 && dw === width && dh === height && inside && sameShape && centred);
  }
}
check("a portrait output keeps the camera's middle strip", (() => {
  const c = cameraCrop(1920, 1080, 1080, 1920);
  return near(c.sh, 1080) && near(c.sw, 1080 * 1080 / 1920) && near(c.sx, (1920 - c.sw) / 2) && c.sy === 0;
})());
check("a portrait camera in a landscape output keeps its middle band", (() => {
  const c = cameraCrop(720, 1280, 1920, 1080);
  return near(c.sw, 720) && near(c.sh, 720 * 1080 / 1920) && near(c.sy, (1280 - c.sh) / 2) && c.sx === 0;
})());
check("a camera with no size yet gives no crop", cameraCrop(0, 0, 1920, 1080) === null);

// No camera: the output is black; the live window shows where it goes.
const none = draw(null);
check("no camera: the output is only black", of(none, "drawImage").length === 0 && texts(none).length === 0 && of(none, "fillRect").length === 1);
check("a camera that has no frame yet draws as none", of(draw(camera(0, 0)), "drawImage").length === 0);
const ed = draw(null, { editor: { safeZones: false } });
check("the live window shows the camera placeholder", texts(ed).includes("Camera") && of(ed, "setLineDash").length === 0);

// The live window's guides, never the output's.
const safe = draw(cam, { editor: { safeZones: true } });
check("Safe Zones draws both zones with their labels", of(safe, "strokeRect").length === 2 && texts(safe).includes("Action safe") && texts(safe).includes("Title safe"));
check("with a camera, no placeholder", !texts(safe).includes("Camera") && of(safe, "drawImage").length === 1);
check("the safe zones sit 5% and 10% in", (() => {
  const [a, b] = of(safe, "strokeRect").map((c) => c.args);
  return near(a[0], 96) && near(a[1], 54) && near(b[0], 192) && near(b[1], 108);
})());

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
