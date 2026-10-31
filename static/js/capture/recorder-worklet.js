// AudioWorkletProcessor "capture-recorder": copies every input block while
// recording and posts them to the main thread in batches, each stamped with
// the global context frame of its first sample. That stamp is what places a
// take on the same clock as playback (05 section 5.4).
//
// Messages in:  {type: "start"} | {type: "stop"}
// Messages out: {type: "started", frame}
//               {type: "batch", frame, channels: Float32Array[]} (transferred)
//               {type: "stopped", frame, frames, dropped}
//
// `frame` in "started" is the context frame of recorded frame 0. "dropped"
// counts render quanta that arrived with no input channels while recording
// (the source was disconnected or not yet flowing); they are recorded as
// silence so the take stays continuous with the clock.

const BATCH_FRAMES = 4096; // about 85 ms at 48 kHz

class CaptureRecorder extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options?.processorOptions || {};
    this.channels = Math.max(1, Math.min(2, opts.channels | 0 || 1));
    this.recording = false;
    this.alive = true;
    this.total = 0;
    this.dropped = 0;
    this._newBatch();
    this.port.onmessage = (e) => {
      const msg = e.data || {};
      if (msg.type === "start") {
        this.recording = true;
        this.startFrame = -1;
        this.total = 0;
        this.dropped = 0;
        this._newBatch();
      } else if (msg.type === "stop") {
        if (this.recording) {
          this._flush();
          this.recording = false;
          this.port.postMessage({
            type: "stopped", frame: this.startFrame, frames: this.total, dropped: this.dropped,
          });
        } else {
          this.port.postMessage({ type: "stopped", frame: -1, frames: 0, dropped: 0 });
        }
      } else if (msg.type === "dispose") {
        this.alive = false;
      }
    };
  }

  _newBatch() {
    this.batch = [];
    for (let c = 0; c < this.channels; c++) this.batch.push(new Float32Array(BATCH_FRAMES));
    this.fill = 0;
    this.batchFrame = -1;
  }

  _flush() {
    if (!this.fill) return;
    const channels = this.batch.map((b) => b.slice(0, this.fill));
    this.port.postMessage(
      { type: "batch", frame: this.batchFrame, channels },
      channels.map((c) => c.buffer),
    );
    this._newBatch();
  }

  process(inputs) {
    if (!this.alive) return false;
    if (!this.recording) return true;
    const input = inputs[0] || [];
    const n = input[0]?.length || 128;
    if (this.startFrame < 0) {
      // currentFrame is the context frame of this quantum's first sample.
      this.startFrame = currentFrame;
      this.port.postMessage({ type: "started", frame: currentFrame });
    }
    if (!input.length) this.dropped++;
    let offset = 0;
    while (offset < n) {
      if (this.batchFrame < 0) this.batchFrame = currentFrame + offset;
      const take = Math.min(n - offset, BATCH_FRAMES - this.fill);
      for (let c = 0; c < this.channels; c++) {
        // A mono source feeding a stereo take is copied to both sides.
        const src = input[c] || input[0];
        if (src) this.batch[c].set(src.subarray(offset, offset + take), this.fill);
        // else: already zero (fresh batch), which records the gap as silence
      }
      this.fill += take;
      offset += take;
      if (this.fill >= BATCH_FRAMES) this._flush();
    }
    this.total += n;
    return true;
  }
}

registerProcessor("capture-recorder", CaptureRecorder);
