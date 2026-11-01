// The phone's microphone capture, on the audio thread (mobile.js ServerSTT:
// capLoad / capFor). Hand-written, no bundler; served as is by the static
// mount, loaded with audioContext.audioWorklet.addModule("/mobile-capture-worklet.js").
//
// "voice-capture": channel 0 of its input, downsampled to processorOptions.rate
// (16 kHz, the rate every clip and stream chunk is uploaded at) and posted
// over its port in blocks of processorOptions.block samples: {x: Float32Array
// (transferred), t: currentTime at the block's end}. The page's main thread
// only queues them: a busy page (the session list, a scroll, the terminal)
// delays a block's delivery, never drops it — a ScriptProcessor's samples
// are lost when its callback cannot run in time.
//
// The downsampler is the page's voiceResample (mobile.js) made continuous:
// every output sample is the Blackman-windowed-sinc weighted average of the
// input around it, cut off at 0.45 of the output rate, normalised by the
// weights it used — what 16 kHz cannot hold is filtered out instead of
// folding back into the speech band. Its history carries over from one
// render quantum (128 frames) to the next, so the blocks join seamlessly.
// Up (a context slower than 16 kHz): linear interpolation. The same rate:
// the samples as they are.
//
// Port messages in: {run: bool} — false stops posting (the page has no one
// listening; the history is dropped, a run starts clean); {stop: true} —
// the node is done (process() returns false).
var SINC_HALF = 16;       // the kernel's half-width, in output samples (mobile.js VOICE_SINC_HALF)
var SINC_CUTOFF = 0.45;   // its cutoff, as a fraction of the output rate (VOICE_SINC_CUTOFF)
var SINC_STEPS = 64;      // its table: entries per input sample (VOICE_SINC_STEPS)

class VoiceCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    var o = (options && options.processorOptions) || {};
    this.to = o.rate > 0 ? o.rate : 16000;
    this.size = o.block > 0 ? Math.floor(o.block) : 680;
    this.step = sampleRate / this.to;          // input samples per output sample
    this.half = SINC_HALF * Math.max(1, this.step);
    if (this.step > 1) {
      var fc = SINC_CUTOFF / this.step, n = Math.ceil(2 * this.half * SINC_STEPS) + 1;
      this.kern = new Float32Array(n);
      for (var i = 0; i < n; i++) {
        var x = i / SINC_STEPS - this.half, u = i / (n - 1);
        var sinc = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
        this.kern[i] = sinc * (0.42 - 0.5 * Math.cos(2 * Math.PI * u) + 0.08 * Math.cos(4 * Math.PI * u));
      }
    }
    this.run = o.run !== false;
    this.alive = true;
    this.reset();
    var self = this;
    this.port.onmessage = function (ev) {
      var d = ev.data || {};
      if (d.stop) { self.alive = false; return; }
      if ("run" in d && !!d.run !== self.run) { self.run = !!d.run; self.reset(); }
    };
  }
  reset() {
    this.hist = new Float32Array(4096);   // input not yet consumed; hist[0] is input sample `base`
    this.len = 0;
    this.base = 0;
    this.k = 0;                           // the next output sample's index (input position k * step)
    this.out = new Float32Array(this.size);
    this.on = 0;
  }
  push(v, t) {
    this.out[this.on++] = v;
    if (this.on < this.size) return;
    var x = this.out;
    this.out = new Float32Array(this.size);
    this.on = 0;
    this.port.postMessage({ x: x, t: t }, [x.buffer]);
  }
  process(inputs) {
    if (!this.alive) return false;
    var ch = inputs[0] && inputs[0][0];
    if (!this.run || !ch || !ch.length) return true;
    var t = currentTime + ch.length / sampleRate, i;
    if (this.step === 1) {
      for (i = 0; i < ch.length; i++) this.push(ch[i], t);
      return true;
    }
    // Append the quantum to the history (grown when it must be).
    if (this.len + ch.length > this.hist.length) {
      var h = new Float32Array(Math.max(this.hist.length * 2, this.len + ch.length));
      h.set(this.hist.subarray(0, this.len));
      this.hist = h;
    }
    this.hist.set(ch, this.len);
    this.len += ch.length;
    var end = this.base + this.len;   // one past the last input sample held
    if (this.step < 1) {
      // Up: linear interpolation between the two input samples around each output.
      for (;;) {
        var p = this.k * this.step, i0 = Math.floor(p);
        if (i0 + 1 >= end) break;
        var a = this.hist[i0 - this.base], b = this.hist[i0 + 1 - this.base];
        this.push(a + (b - a) * (p - i0), t);
        this.k++;
      }
    } else {
      for (;;) {
        var c = this.k * this.step, hi = Math.floor(c + this.half);
        if (hi >= end) break;
        var lo = Math.max(0, Math.ceil(c - this.half)), acc = 0, wsum = 0;
        for (var j = lo; j <= hi; j++) {
          var w = this.kern[Math.round((j - c + this.half) * SINC_STEPS)];
          acc += this.hist[j - this.base] * w;
          wsum += w;
        }
        this.push(wsum ? acc / wsum : 0, t);
        this.k++;
      }
    }
    // Drop the input no later output sample reaches.
    var keep = Math.max(this.base, Math.min(end, Math.floor(this.k * this.step - this.half) - 1));
    if (keep > this.base) {
      this.hist.copyWithin(0, keep - this.base, this.len);
      this.len -= keep - this.base;
      this.base = keep;
    }
    return true;
  }
}

registerProcessor("voice-capture", VoiceCapture);
