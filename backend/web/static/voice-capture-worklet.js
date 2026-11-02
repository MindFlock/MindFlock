//#region src/lib/voiceCapture.worklet.ts
const CAPTURE_PROCESSOR = "mf-voice-capture";
const CAPTURE_BLOCK_FRAMES = 4096;
const CAPTURE_RATE = 16e3;
const SINC_HALF_ZC = 16;
const SINC_ROLLOFF = .9;
const MAX_PHASES = 2048;
const gcd = (a, b) => b ? gcd(b, a % b) : a;
var StreamResampler = class {
	fromRate;
	toRate;
	L;
	M;
	half;
	K;
	fc;
	table;
	buf;
	len = 0;
	base;
	next = 0;
	constructor(fromRate, toRate) {
		this.fromRate = fromRate;
		this.toRate = toRate;
		const from = Math.round(fromRate);
		const to = Math.round(toRate);
		const g = gcd(from, to) || 1;
		this.L = to / g;
		this.M = from / g;
		const ratio = Math.min(1, to / from);
		this.fc = .5 * ratio * SINC_ROLLOFF;
		this.half = SINC_HALF_ZC / ratio;
		this.K = Math.ceil(this.half);
		this.base = -this.K;
		this.buf = new Float32Array(Math.max(1024, 4 * this.K + 512));
		this.len = this.K;
		this.table = from === to || this.L > MAX_PHASES ? null : Array.from({ length: this.L }, (_, p) => this.taps(p / this.L));
	}
	taps(frac) {
		const K = this.K;
		const out = new Float32Array(2 * K);
		let sum = 0;
		for (let k = -K + 1; k <= K; k++) {
			const x = k - frac;
			const u = x / this.half;
			if (u < -1 || u > 1) continue;
			const win = .42 + .5 * Math.cos(Math.PI * u) + .08 * Math.cos(2 * Math.PI * u);
			const arg = 2 * this.fc * x;
			const w = (arg === 0 ? 1 : Math.sin(Math.PI * arg) / (Math.PI * arg)) * win;
			out[k + K - 1] = w;
			sum += w;
		}
		if (sum) for (let i = 0; i < out.length; i++) out[i] /= sum;
		return out;
	}
	push(input) {
		if (this.fromRate === this.toRate) return input.slice();
		this.append(input);
		const { L, M, K } = this;
		const end = this.base + this.len;
		const last = Math.floor((end - 1 - K) * L / M);
		const n = Math.max(0, last - this.next + 1);
		const out = new Float32Array(n);
		for (let o = 0; o < n; o++) {
			const pos = (this.next + o) * M;
			const c = Math.floor(pos / L);
			const ph = pos - c * L;
			const taps = this.table ? this.table[ph] : this.taps(ph / L);
			const at = c - K + 1 - this.base;
			let acc = 0;
			for (let k = 0; k < taps.length; k++) acc += this.buf[at + k] * taps[k];
			out[o] = acc;
		}
		this.next += n;
		const keepFrom = Math.floor(this.next * M / L) - K + 1;
		const drop = Math.min(this.len, Math.max(0, keepFrom - this.base));
		if (drop) {
			this.buf.copyWithin(0, drop, this.len);
			this.len -= drop;
			this.base += drop;
		}
		return out;
	}
	append(input) {
		if (this.len + input.length > this.buf.length) {
			const grown = new Float32Array(Math.max(this.buf.length * 2, this.len + input.length));
			grown.set(this.buf.subarray(0, this.len));
			this.buf = grown;
		}
		this.buf.set(input, this.len);
		this.len += input.length;
	}
};
var CaptureCore = class {
	inRate;
	emit;
	outRate;
	blockFrames;
	rs;
	parts = [];
	outLen = 0;
	inBlock = 0;
	sumSq = 0;
	frames = 0;
	constructor(inRate, emit, outRate = CAPTURE_RATE, blockFrames = CAPTURE_BLOCK_FRAMES) {
		this.inRate = inRate;
		this.emit = emit;
		this.outRate = outRate;
		this.blockFrames = blockFrames;
		this.rs = new StreamResampler(inRate, outRate);
	}
	push(input) {
		let from = 0;
		while (from < input.length) {
			const n = Math.min(input.length - from, this.blockFrames - this.inBlock);
			const part = from === 0 && n === input.length ? input : input.subarray(from, from + n);
			for (let i = 0; i < part.length; i++) this.sumSq += part[i] * part[i];
			const out = this.rs.push(part);
			if (out.length) {
				this.parts.push(out);
				this.outLen += out.length;
			}
			this.inBlock += n;
			this.frames += n;
			from += n;
			if (this.inBlock >= this.blockFrames) this.flush();
		}
	}
	flush() {
		const pcm = new Float32Array(this.outLen);
		let at = 0;
		for (const p of this.parts) {
			pcm.set(p, at);
			at += p.length;
		}
		const rms = this.inBlock ? Math.min(1, Math.sqrt(this.sumSq / this.inBlock)) : 0;
		this.parts = [];
		this.outLen = 0;
		this.inBlock = 0;
		this.sumSq = 0;
		this.emit({
			pcm,
			rms,
			frames: this.frames
		});
	}
};
if (typeof registerProcessor === "function" && typeof AudioWorkletProcessor === "function") registerProcessor(CAPTURE_PROCESSOR, class extends AudioWorkletProcessor {
	core;
	stopped = false;
	constructor(options) {
		super(options);
		const o = options?.processorOptions || {};
		this.core = new CaptureCore(sampleRate, (b) => this.port.postMessage(b, [b.pcm.buffer]), o.outRate || 16e3, o.blockFrames || 4096);
		this.port.onmessage = (e) => {
			if (e.data === "stop") this.stopped = true;
		};
	}
	process(inputs) {
		if (this.stopped) return false;
		const ch = inputs[0]?.[0];
		if (ch?.length) this.core.push(ch);
		return true;
	}
});
//#endregion
