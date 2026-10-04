const TAU = Math.PI * 2;
function rng(seed) {
	let s = seed >>> 0 || 1;
	return () => {
		s ^= s << 13;
		s >>>= 0;
		s ^= s >> 17;
		s ^= s << 5;
		s >>>= 0;
		return s / 4294967296;
	};
}
function hashStr(str) {
	let h = 2166136261;
	for (let i = 0; i < str.length; i++) {
		h ^= str.charCodeAt(i);
		h = Math.imul(h, 16777619);
	}
	return h >>> 0;
}
function h2(str) {
	let a = 2166136261, b = -624069552;
	for (let i = 0; i < str.length; i++) {
		const c = str.charCodeAt(i);
		a ^= c;
		a = Math.imul(a, 16777619);
		b = Math.imul(b ^ c, 16777619) + (b >>> 13);
	}
	return (a >>> 0).toString(36) + (b >>> 0).toString(36);
}
const cmpStr = (a, b) => a < b ? -1 : a > b ? 1 : 0;
function mkDisc(x, y, rho, t) {
	return {
		k: 0,
		x,
		y,
		rho,
		t,
		x0: 0,
		y0: 0,
		x1: 0,
		y1: 0,
		hw: 0,
		sf: 0,
		ef: 0,
		term: null,
		bx0: x - rho,
		by0: y - rho,
		bx1: x + rho,
		by1: y + rho,
		d: 0,
		sub: -1,
		dead: false,
		m: 0,
		st: 0
	};
}
function mkSeg(x0, y0, x1, y1, hw, sf, ef, term) {
	return {
		k: 1,
		x: 0,
		y: 0,
		rho: 0,
		t: null,
		x0,
		y0,
		x1,
		y1,
		hw,
		sf,
		ef,
		term,
		bx0: (x0 < x1 ? x0 : x1) - hw,
		by0: (y0 < y1 ? y0 : y1) - hw,
		bx1: (x0 > x1 ? x0 : x1) + hw,
		by1: (y0 > y1 ? y0 : y1) + hw,
		d: 0,
		sub: -1,
		dead: false,
		m: 0,
		st: 0
	};
}
function setSeg(q, x0, y0, x1, y1, hw, sf, ef, term) {
	q.x0 = x0;
	q.y0 = y0;
	q.x1 = x1;
	q.y1 = y1;
	q.hw = hw;
	q.sf = sf;
	q.ef = ef;
	q.term = term;
	q.bx0 = (x0 < x1 ? x0 : x1) - hw;
	q.by0 = (y0 < y1 ? y0 : y1) - hw;
	q.bx1 = (x0 > x1 ? x0 : x1) + hw;
	q.by1 = (y0 > y1 ? y0 : y1) + hw;
}
const mkPool = (n) => Array.from({ length: n }, () => mkSeg(0, 0, 0, 0, 0, 0, 0, null));
const SWEEP_POOL = mkPool(5);
const LOC_POOL = mkPool(1)[0];
const COMPACT_POOL = mkPool(5);
function clonePrim(p, ox, oy) {
	return p.k === 0 ? mkDisc(p.x + ox, p.y + oy, p.rho, p.t) : mkSeg(p.x0 + ox, p.y0 + oy, p.x1 + ox, p.y1 + oy, p.hw, p.sf, p.ef, p.term);
}
const PK = {
	gapTT: .8,
	gapTW: 1.6,
	gapWW: 1.8,
	CS: 16,
	ax: 1,
	ay: 1,
	down: .4,
	heart: 1,
	compact: 6,
	cstep: .07,
	stemR: false,
	rtop: 9,
	cmax: 9,
	leader: 0,
	leaderOrder: "mid",
	cwx: 1,
	cwy: 1,
	flat: 0,
	flatA: .8
};
const CROWN = {
	ax: 1,
	ay: 1,
	down: .4,
	heart: 1,
	lim: 1.4,
	stemR: false,
	rtop: 9,
	cmax: 9,
	trunk: .42,
	leader: 0,
	leaderOrder: "mid",
	cheart: 1,
	cwx: 1,
	cwy: 1,
	flat: 0,
	flatA: .8
};
const CROWN_BIG = {
	min: 2500,
	cwx: 2.5,
	cwy: .5,
	cmax: 1.1
};
const ROOTS = {
	ax: 1,
	ay: 2.2,
	down: 0,
	heart: .5,
	lim: 1.4,
	stemR: false,
	rtop: 9,
	cmax: 9,
	leader: 0,
	leaderOrder: "mid",
	cwx: 1,
	cwy: 1,
	flat: 0,
	flatA: .8
};
const LEAN = {
	k: .6,
	lim: 1.4
};
function pkUse(P) {
	PK.ax = P.ax;
	PK.ay = P.ay;
	PK.down = P.down;
	PK.heart = P.heart;
	LEAN.lim = P.lim;
	PK.stemR = P.stemR;
	PK.rtop = P.rtop;
	PK.cmax = P.cmax;
	PK.leader = P.leader;
	PK.leaderOrder = P.leaderOrder;
	PK.cwx = P.cwx;
	PK.cwy = P.cwy;
	PK.flat = P.flat;
	PK.flatA = P.flatA;
}
let PK_FORK = 0;
let PK_STAMP = 0;
let PK_IG0 = -1;
let PK_IG1 = -1;
let PK_ONLY = false;
let CTX;
function newCtx(kW) {
	return {
		kW,
		cache: null,
		ci: 0,
		unions: [],
		memo: /* @__PURE__ */ new Map(),
		memoHits: 0,
		final: null,
		warmFailed: false,
		resolved: 0,
		prevSigs: null,
		sigs: [],
		splits: /* @__PURE__ */ new Map(),
		prevSplits: null,
		orders: /* @__PURE__ */ new Map(),
		prevOrders: null,
		passes: PK.compact,
		quantise: false,
		phase: 0,
		work: 0,
		workTotal: 1,
		stats: {
			hits: 0,
			pens: 0,
			sweeps: 0
		}
	};
}
function unionRecs(ctx) {
	return ctx.unions.map((U) => U.place.flatMap((p) => [
		p.Ex,
		p.Ey,
		p.rho,
		p.f
	]));
}
function finalRecs(ctx) {
	return ctx.unions.map((U) => [
		U.skey,
		U.place.flatMap((p) => [
			p.Ex,
			p.Ey,
			p.rho,
			p.f
		]),
		U.sig
	]);
}
function beginBuild(ctx) {
	CTX = ctx;
	PK_FORK = 0;
}
var Grid = class {
	i0 = 0;
	j0 = 0;
	ni = 0;
	nj = 0;
	lists = [];
	occ;
	constructor(_cap = 256) {
		this.occ = new Occ();
	}
	reserve(i0, j0, i1, j1) {
		if (this.ni && i0 >= this.i0 && j0 >= this.j0 && i1 < this.i0 + this.ni && j1 < this.j0 + this.nj) return;
		let a0 = i0, b0 = j0, a1 = i1, b1 = j1;
		if (this.ni) {
			a0 = Math.min(a0, this.i0);
			b0 = Math.min(b0, this.j0);
			a1 = Math.max(a1, this.i0 + this.ni - 1);
			b1 = Math.max(b1, this.j0 + this.nj - 1);
			const mi = Math.max(8, a1 - a0 + 1 >> 1), mj = Math.max(8, b1 - b0 + 1 >> 1);
			if (i0 < this.i0) a0 -= mi;
			if (i1 >= this.i0 + this.ni) a1 += mi;
			if (j0 < this.j0) b0 -= mj;
			if (j1 >= this.j0 + this.nj) b1 += mj;
		}
		const ni = a1 - a0 + 1, nj = b1 - b0 + 1;
		const lists = new Array(ni * nj);
		for (let i = 0; i < this.ni; i++) for (let j = 0; j < this.nj; j++) {
			const v = this.lists[i * this.nj + j];
			if (v) lists[(i + this.i0 - a0) * nj + (j + this.j0 - b0)] = v;
		}
		this.i0 = a0;
		this.j0 = b0;
		this.ni = ni;
		this.nj = nj;
		this.lists = lists;
	}
	purge() {
		const L = this.lists;
		for (let k = 0; k < L.length; k++) {
			const a = L[k];
			if (!a) continue;
			let w = 0;
			for (let r = 0; r < a.length; r++) if (!a[r].dead) a[w++] = a[r];
			if (w === 0) L[k] = void 0;
			else a.length = w;
		}
	}
	push(i, j, p) {
		if (!this.ni || i < this.i0 || j < this.j0 || i >= this.i0 + this.ni || j >= this.j0 + this.nj) this.reserve(i, j, i, j);
		const k = (i - this.i0) * this.nj + (j - this.j0);
		const a = this.lists[k];
		if (!a) this.lists[k] = [p];
		else if (a[a.length - 1] !== p) a.push(p);
	}
};
var Occ = class {
	i0 = 0;
	j0 = 0;
	ni = 0;
	nj = 0;
	v = /* @__PURE__ */ new Int32Array(0);
	constructor(_cap = 0) {}
	get(i, j) {
		const a = i - this.i0, b = j - this.j0;
		return a < 0 || b < 0 || a >= this.ni || b >= this.nj ? 0 : this.v[a * this.nj + b];
	}
	add(i, j, d) {
		if (!this.ni || i < this.i0 || j < this.j0 || i >= this.i0 + this.ni || j >= this.j0 + this.nj) {
			let a0 = i, b0 = j, a1 = i, b1 = j;
			if (this.ni) {
				a0 = Math.min(a0, this.i0);
				b0 = Math.min(b0, this.j0);
				a1 = Math.max(a1, this.i0 + this.ni - 1);
				b1 = Math.max(b1, this.j0 + this.nj - 1);
				const mi = Math.max(4, a1 - a0 + 1 >> 1), mj = Math.max(4, b1 - b0 + 1 >> 1);
				if (i < this.i0) a0 -= mi;
				if (i >= this.i0 + this.ni) a1 += mi;
				if (j < this.j0) b0 -= mj;
				if (j >= this.j0 + this.nj) b1 += mj;
			} else {
				a0 -= 4;
				b0 -= 4;
				a1 += 4;
				b1 += 4;
			}
			const ni = a1 - a0 + 1, nj = b1 - b0 + 1;
			const v = new Int32Array(ni * nj);
			for (let x = 0; x < this.ni; x++) for (let y = 0; y < this.nj; y++) {
				const c = this.v[x * this.nj + y];
				if (c) v[(x + this.i0 - a0) * nj + (y + this.j0 - b0)] = c;
			}
			this.i0 = a0;
			this.j0 = b0;
			this.ni = ni;
			this.nj = nj;
			this.v = v;
		}
		this.v[(i - this.i0) * this.nj + (j - this.j0)] += d;
	}
};
const OCC_CS = 32;
const OCC_PAD = 1.5;
function occBox(p, ox, oy, out) {
	const x0 = p.k === 0 ? p.x - p.rho : p.bx0, y0 = p.k === 0 ? p.y - p.rho : p.by0, x1 = p.k === 0 ? p.x + p.rho : p.bx1, y1 = p.k === 0 ? p.y + p.rho : p.by1;
	out[0] = Math.floor((x0 + ox - OCC_PAD) / OCC_CS);
	out[1] = Math.floor((y0 + oy - OCC_PAD) / OCC_CS);
	out[2] = Math.floor((x1 + ox + OCC_PAD) / OCC_CS);
	out[3] = Math.floor((y1 + oy + OCC_PAD) / OCC_CS);
}
const OB = [
	0,
	0,
	0,
	0
];
function occAdd(o, p, d) {
	occBox(p, 0, 0, OB);
	for (let i = OB[0]; i <= OB[2]; i++) for (let j = OB[1]; j <= OB[3]; j++) o.add(i, j, d);
}
let OWN = null;
function occMay(G, p, ox, oy) {
	occBox(p, ox, oy, OB);
	return occMayCells(G.occ);
}
function occMayBox(G, x0, y0, x1, y1) {
	OB[0] = Math.floor((x0 - OCC_PAD) / OCC_CS);
	OB[1] = Math.floor((y0 - OCC_PAD) / OCC_CS);
	OB[2] = Math.floor((x1 + OCC_PAD) / OCC_CS);
	OB[3] = Math.floor((y1 + OCC_PAD) / OCC_CS);
	return occMayCells(G.occ);
}
function occMayCells(o) {
	const i0 = Math.max(OB[0], o.i0), i1 = Math.min(OB[2], o.i0 + o.ni - 1), j0 = Math.max(OB[1], o.j0), j1 = Math.min(OB[3], o.j0 + o.nj - 1);
	const nj = o.nj, v = o.v;
	const own = OWN;
	for (let i = i0; i <= i1; i++) {
		const row = (i - o.i0) * nj - o.j0;
		for (let j = j0; j <= j1; j++) {
			const c = v[row + j];
			if (own) {
				const w = own.get(i, j);
				if (PK_ONLY ? w > 0 : c - w > 0) return true;
			} else if (c > 0) return true;
		}
	}
	return false;
}
const pkKey = (i, j) => i + 32768 << 16 | j + 32768 & 65535;
function pkSpan(p, ox, oy, out) {
	const CS = PK.CS;
	out.length = 0;
	if (p.k === 0) {
		const r = p.rho + 2, x = p.x + ox, y = p.y + oy;
		out.push(Math.floor((x - r) / CS), Math.floor((y - r) / CS), Math.floor((x + r) / CS), Math.floor((y + r) / CS));
		return out;
	}
	const r = p.hw + 2, ax = p.x0 + ox, ay = p.y0 + oy, bx = p.x1 + ox, by = p.y1 + oy;
	const n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / (CS * 3)));
	for (let k = 0; k < n; k++) {
		const x0 = ax + (bx - ax) * k / n, y0 = ay + (by - ay) * k / n, x1 = ax + (bx - ax) * (k + 1) / n, y1 = ay + (by - ay) * (k + 1) / n;
		out.push(Math.floor((Math.min(x0, x1) - r) / CS), Math.floor((Math.min(y0, y1) - r) / CS), Math.floor((Math.max(x0, x1) + r) / CS), Math.floor((Math.max(y0, y1) + r) / CS));
	}
	return out;
}
const PK_SPAN = [];
function pkAdd(G, p) {
	occAdd(G.occ, p, 1);
	const sp = pkSpan(p, 0, 0, PK_SPAN);
	for (let s = 0; s < sp.length; s += 4) {
		G.reserve(sp[s], sp[s + 1], sp[s + 2], sp[s + 3]);
		for (let i = sp[s]; i <= sp[s + 2]; i++) for (let j = sp[s + 1]; j <= sp[s + 3]; j++) G.push(i, j, p);
	}
}
function dPS(px, py, ax, ay, bx, by) {
	const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
	let t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
	t = t < 0 ? 0 : t > 1 ? 1 : t;
	const ex = px - ax - dx * t, ey = py - ay - dy * t;
	return Math.sqrt(ex * ex + ey * ey);
}
const orient = (px, py, qx, qy, rx, ry) => (qx - px) * (ry - py) - (qy - py) * (rx - px);
function dSS(ax, ay, bx, by, cx, cy, dx, dy) {
	const o1 = orient(ax, ay, bx, by, cx, cy), o2 = orient(ax, ay, bx, by, dx, dy), o3 = orient(cx, cy, dx, dy, ax, ay), o4 = orient(cx, cy, dx, dy, bx, by);
	if ((o1 > 0 && o2 < 0 || o1 < 0 && o2 > 0) && (o3 > 0 && o4 < 0 || o3 < 0 && o4 > 0)) return 0;
	return Math.min(dPS(ax, ay, cx, cy, dx, dy), dPS(bx, by, cx, cy, dx, dy), dPS(cx, cy, ax, ay, bx, by), dPS(dx, dy, ax, ay, bx, by));
}
const PKC = {
	x: 0,
	y: 0,
	need: 0
};
let CPX = 0;
let CPY = 0;
function dPSc(px, py, ax, ay, bx, by) {
	const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
	let t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
	t = t < 0 ? 0 : t > 1 ? 1 : t;
	CPX = ax + dx * t;
	CPY = ay + dy * t;
	const ex = px - CPX, ey = py - CPY;
	return Math.sqrt(ex * ex + ey * ey);
}
function pkPen(p, ox, oy, q) {
	if (p.k === 0) {
		const px = p.x + ox, py = p.y + oy;
		if (q.k === 0) {
			const need = p.rho + q.rho + PK.gapTT, dx = px - q.x, dy = py - q.y, d2 = dx * dx + dy * dy;
			if (d2 >= need * need) return 0;
			PKC.x = dx;
			PKC.y = dy;
			PKC.need = need;
			return need - Math.sqrt(d2);
		}
		if (q.term === p.t) return 0;
		const need = p.rho + q.hw + PK.gapTW;
		const gx = px < q.bx0 ? q.bx0 - px : px > q.bx1 ? px - q.bx1 : 0, gy = py < q.by0 ? q.by0 - py : py > q.by1 ? py - q.by1 : 0;
		if (gx * gx + gy * gy >= (need - q.hw) * (need - q.hw) && (gx > 0 || gy > 0)) return 0;
		const d = dPSc(px, py, q.x0, q.y0, q.x1, q.y1);
		if (d >= need) return 0;
		PKC.x = px - CPX;
		PKC.y = py - CPY;
		PKC.need = need;
		return need - d;
	}
	const ax = p.x0 + ox, ay = p.y0 + oy, bx = p.x1 + ox, by = p.y1 + oy;
	if (q.k === 0) {
		if (p.term === q.t) return 0;
		const need = q.rho + p.hw + PK.gapTW;
		const px0 = p.bx0 + ox + p.hw, px1 = p.bx1 + ox - p.hw, py0 = p.by0 + oy + p.hw, py1 = p.by1 + oy - p.hw;
		const gx = q.x < px0 ? px0 - q.x : q.x > px1 ? q.x - px1 : 0, gy = q.y < py0 ? py0 - q.y : q.y > py1 ? q.y - py1 : 0;
		if (gx * gx + gy * gy >= need * need) return 0;
		const d = dPSc(q.x, q.y, ax, ay, bx, by);
		if (d >= need) return 0;
		PKC.x = CPX - q.x;
		PKC.y = CPY - q.y;
		PKC.need = need;
		return need - d;
	}
	if (p.sf === q.sf || p.ef === q.sf || q.ef === p.sf) return 0;
	const need = p.hw + q.hw + PK.gapWW;
	{
		const px0 = p.bx0 + ox + p.hw, px1 = p.bx1 + ox - p.hw, py0 = p.by0 + oy + p.hw, py1 = p.by1 + oy - p.hw;
		const qx0 = q.bx0 + q.hw, qx1 = q.bx1 - q.hw, qy0 = q.by0 + q.hw, qy1 = q.by1 - q.hw;
		const gx = px0 > qx1 ? px0 - qx1 : qx0 > px1 ? qx0 - px1 : 0, gy = py0 > qy1 ? py0 - qy1 : qy0 > py1 ? qy0 - py1 : 0;
		if (gx * gx + gy * gy >= need * need) return 0;
	}
	const d = dSS(ax, ay, bx, by, q.x0, q.y0, q.x1, q.y1);
	if (d >= need) return 0;
	let best = 0xde0b6b3a7640000, vx = 0, vy = 0, e;
	e = dPSc(ax, ay, q.x0, q.y0, q.x1, q.y1);
	if (e < best) {
		best = e;
		vx = ax - CPX;
		vy = ay - CPY;
	}
	e = dPSc(bx, by, q.x0, q.y0, q.x1, q.y1);
	if (e < best) {
		best = e;
		vx = bx - CPX;
		vy = by - CPY;
	}
	e = dPSc(q.x0, q.y0, ax, ay, bx, by);
	if (e < best) {
		best = e;
		vx = CPX - q.x0;
		vy = CPY - q.y0;
	}
	e = dPSc(q.x1, q.y1, ax, ay, bx, by);
	if (e < best) {
		best = e;
		vx = CPX - q.x1;
		vy = CPY - q.y1;
	}
	PKC.x = d === 0 ? 0 : vx;
	PKC.y = d === 0 ? 0 : vy;
	PKC.need = need;
	return need - d;
}
function pkHit(G, p, ox, oy) {
	if (!occMay(G, p, ox, oy)) return 0;
	const st = ++PK_STAMP, CS = PK.CS;
	if (p.k === 0) {
		const r = p.rho + 2, x = p.x + ox, y = p.y + oy;
		return scanCells(G, p, ox, oy, st, Math.floor((x - r) / CS), Math.floor((y - r) / CS), Math.floor((x + r) / CS), Math.floor((y + r) / CS));
	}
	const r = p.hw + 2, ax = p.x0 + ox, ay = p.y0 + oy, bx = p.x1 + ox, by = p.y1 + oy;
	const n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / (CS * 3)));
	for (let k = 0; k < n; k++) {
		const x0 = ax + (bx - ax) * k / n, y0 = ay + (by - ay) * k / n, x1 = ax + (bx - ax) * (k + 1) / n, y1 = ay + (by - ay) * (k + 1) / n;
		const v = scanCells(G, p, ox, oy, st, Math.floor((Math.min(x0, x1) - r) / CS), Math.floor((Math.min(y0, y1) - r) / CS), Math.floor((Math.max(x0, x1) + r) / CS), Math.floor((Math.max(y0, y1) + r) / CS));
		if (v) return v;
	}
	return 0;
}
function scanCells(G, p, ox, oy, st, i0, j0, i1, j1) {
	if (i0 < G.i0) i0 = G.i0;
	if (j0 < G.j0) j0 = G.j0;
	if (i1 > G.i0 + G.ni - 1) i1 = G.i0 + G.ni - 1;
	if (j1 > G.j0 + G.nj - 1) j1 = G.j0 + G.nj - 1;
	const nj = G.nj, lists = G.lists;
	for (let i = i0; i <= i1; i++) {
		const row = (i - G.i0) * nj - G.j0;
		for (let j = j0; j <= j1; j++) {
			const a = lists[row + j];
			if (!a) continue;
			for (let k = 0; k < a.length; k++) {
				const q = a[k];
				if (q.st === st) continue;
				q.st = st;
				if (q.sub !== -1 && (q.dead || (q.sub >= PK_IG0 && q.sub < PK_IG1) !== PK_ONLY)) continue;
				const v = pkPen(p, ox, oy, q);
				if (v > 0) return v;
			}
		}
	}
	return 0;
}
function pkDist(p) {
	return p.k === 0 ? Math.max(0, Math.hypot(p.x, p.y) - p.rho) : Math.min(Math.hypot(p.x0, p.y0), Math.hypot(p.x1, p.y1)) - p.hw;
}
const STEM = {
	a0: .9,
	a1: 1.3,
	k0: .42,
	k1: .22,
	Lref: 180
};
function stemCP(Ex, Ey, rho) {
	const L = Math.hypot(Ex, Ey) || 1, ux = Ex / L, uy = Ey / L, kL = Math.min(1, STEM.Lref / L);
	let t0x = ux, t0y = uy - STEM.a0 * kL;
	const l0 = Math.hypot(t0x, t0y) || 1;
	t0x /= l0;
	t0y /= l0;
	let t1x = ux + STEM.a1 * kL * Math.sin(rho), t1y = uy - STEM.a1 * kL * Math.cos(rho);
	const l1 = Math.hypot(t1x, t1y) || 1;
	t1x /= l1;
	t1y /= l1;
	const k0 = L * STEM.k0, k1 = L * STEM.k1;
	return [
		0,
		0,
		t0x * k0,
		t0y * k0,
		Ex - t1x * k1,
		Ey - t1y * k1,
		Ex,
		Ey
	];
}
function bez(c, t) {
	const mt = 1 - t;
	return [mt * mt * mt * c[0] + 3 * mt * mt * t * c[2] + 3 * mt * t * t * c[4] + t * t * t * c[6], mt * mt * mt * c[1] + 3 * mt * mt * t * c[3] + 3 * mt * t * t * c[5] + t * t * t * c[7]];
}
const taperHW = (wb, we, t) => (wb + (we - wb) * Math.pow(t, .9)) / 2;
const TAP5 = [
	0,
	1,
	2,
	3,
	4,
	5
].map((i) => Math.pow(i / 5, .9));
function pkStemSegs(S, c, fid, pool) {
	const out = pool || [], n = 5;
	let px = c[0], py = c[1];
	const ef = S.fork != null ? S.fork : S.efId;
	for (let i = 1; i <= n; i++) {
		const q = bez(c, i / n);
		const hw = Math.max(S.wb + (S.we - S.wb) * TAP5[i - 1], S.wb + (S.we - S.wb) * TAP5[i]) / 2 + .35;
		if (pool) setSeg(pool[i - 1], px, py, q[0], q[1], hw, fid, ef, S.term || null);
		else out.push(mkSeg(px, py, q[0], q[1], hw, fid, ef, S.term || null));
		px = q[0];
		py = q[1];
	}
	return out;
}
function pkRotPrim(p, c, s, f) {
	const q = p.k === 0 ? mkDisc(c * f * p.x - s * p.y, s * f * p.x + c * p.y, p.rho, p.t) : mkSeg(c * f * p.x0 - s * p.y0, s * f * p.x0 + c * p.y0, c * f * p.x1 - s * p.y1, s * f * p.x1 + c * p.y1, p.hw, p.sf, p.ef, p.term);
	q.d = p.d;
	return q;
}
function pkRotated(S, rho, f) {
	if (!rho && f === 1) return S.prims;
	const key = Math.round(rho * 1e4) * 2 + (f < 0 ? 1 : 0);
	if (!S.rc) S.rc = /* @__PURE__ */ new Map();
	let P = S.rc.get(key);
	if (P) return P;
	const c = Math.cos(rho), s = Math.sin(rho);
	P = S.prims.map((p) => pkRotPrim(p, c, s, f));
	S.rc.set(key, P);
	return P;
}
function pkMom(S, rho, f) {
	const c = Math.cos(rho), s = Math.sin(rho), sx = f * S.sx, sxy = f * S.sxy;
	return {
		sx: c * sx - s * S.sy,
		sy: s * sx + c * S.sy,
		sxx: c * c * S.sxx - 2 * c * s * sxy + s * s * S.syy,
		syy: s * s * S.sxx + 2 * c * s * sxy + c * c * S.syy,
		sxy: c * s * (S.sxx - S.syy) + (c * c - s * s) * sxy
	};
}
function pkCost(S, mo, Ex, Ey, h) {
	const cy = Ey + mo.sy / S.m, Ey2 = Ey + h;
	let c = (mo.sxx + 2 * Ex * mo.sx + S.m * Ex * Ex) * PK.ax + (mo.syy + 2 * Ey2 * mo.sy + S.m * Ey2 * Ey2) * PK.ay + PK.down * S.m * Math.max(0, cy) * Math.abs(cy);
	if (PK.flat) {
		const e = Math.max(0, Math.abs(Math.atan2(Ex, -Ey)) - PK.flatA);
		c += PK.flat * S.m * h * h * e * e;
	}
	return c;
}
const BUCKET_CS = 64;
function bucketsOf(P) {
	const ids = /* @__PURE__ */ new Map();
	const of = new Int32Array(P.length);
	const boxes = [];
	for (let i = 0; i < P.length; i++) {
		const p = P[i];
		const x0 = p.k === 0 ? p.x - p.rho : p.bx0, y0 = p.k === 0 ? p.y - p.rho : p.by0, x1 = p.k === 0 ? p.x + p.rho : p.bx1, y1 = p.k === 0 ? p.y + p.rho : p.by1;
		const key = pkKey(Math.floor((x0 + x1) / 2 / BUCKET_CS), Math.floor((y0 + y1) / 2 / BUCKET_CS));
		let b = ids.get(key);
		if (b === void 0) {
			b = ids.size;
			ids.set(key, b);
			boxes.push(x0, y0, x1, y1);
		} else {
			const o = b * 4;
			if (x0 < boxes[o]) boxes[o] = x0;
			if (y0 < boxes[o + 1]) boxes[o + 1] = y0;
			if (x1 > boxes[o + 2]) boxes[o + 2] = x1;
			if (y1 > boxes[o + 3]) boxes[o + 3] = y1;
		}
		of[i] = b;
	}
	return {
		of,
		box: Float64Array.from(boxes),
		flag: new Int32Array(ids.size),
		gen: 0
	};
}
function pkSweep(S, psi, rho, f, Phi, l0, fid, maxL, costCap, costBase, h) {
	CTX.stats.sweeps++;
	const ux = Math.sin(psi), uy = -Math.cos(psi);
	const P = pkRotated(S, rho, f), mo = pkMom(S, rho, f), cr = Math.cos(-rho), sr = Math.sin(-rho);
	let l = l0, selfN = 0;
	for (let it = 0; it < 120 && l <= maxL; it++) {
		const Ex = ux * l, Ey = uy * l;
		if (costCap !== void 0 && costBase + pkCost(S, mo, Ex, Ey, h) >= costCap) return null;
		const c = stemCP(Ex, Ey, rho);
		const stem = pkStemSegs(S, c, fid, SWEEP_POOL);
		let pen = 0, self = false, frac = 1;
		for (let i = 0; i < stem.length && !pen; i++) {
			pen = pkHit(Phi, stem[i], 0, 0);
			if (pen) frac = Math.max(.35, (i + .5) / stem.length);
		}
		if (!pen && S.grid) for (let i = 0; i < stem.length && !pen; i++) {
			const g = stem[i];
			const loc = LOC_POOL;
			setSeg(loc, f * (cr * (g.x0 - Ex) - sr * (g.y0 - Ey)), sr * (g.x0 - Ex) + cr * (g.y0 - Ey), f * (cr * (g.x1 - Ex) - sr * (g.y1 - Ey)), sr * (g.x1 - Ex) + cr * (g.y1 - Ey), g.hw, g.sf, g.ef, g.term);
			pen = pkHit(S.grid, loc, 0, 0);
			if (pen && i >= 3) self = true;
		}
		if (!pen) for (let i = 0; i < P.length && !pen; i++) pen = pkHit(Phi, P[i], Ex, Ey);
		if (!pen) return {
			l,
			Ex,
			Ey,
			c,
			stem: stem.map((q) => clonePrim(q, 0, 0)),
			rho,
			f,
			mo
		};
		if (self && ++selfN > 4) return null;
		const dot = PKC.x * ux + PKC.y * uy, dd = PKC.x * PKC.x + PKC.y * PKC.y;
		let step = -dot + Math.sqrt(Math.max(0, dot * dot - dd + PKC.need * PKC.need));
		if (self) step = pen;
		else step /= frac;
		l += Math.max(.6, Math.min(step + .15, 400), l * .015);
	}
	return null;
}
function pkTerm(node, own, files, region) {
	const n = files.length;
	const s = 10 * (region === "root" ? .84 : .92);
	const rnd = rng(hashStr(node.path + (own ? "#" : "") + region));
	const K = Math.ceil(Math.sqrt(Math.max(1, n))) + 2;
	const pts = [];
	for (let j = -K; j <= K; j++) for (let i = -K; i <= K; i++) {
		const x = (i + (j & 1) * .5) * s, y = j * s * .866;
		pts.push([
			x,
			y,
			Math.hypot(x, y * 1.08) + (rnd() - .5) * .01
		]);
	}
	pts.sort((a, b) => a[2] - b[2]);
	const P = pts.slice(0, n);
	let cx = 0, cy = 0;
	for (const p of P) {
		cx += p[0];
		cy += p[1];
	}
	cx /= Math.max(1, n);
	cy /= Math.max(1, n);
	const loc = P.map((p) => [p[0] - cx + (rnd() - .5) * s * .3, p[1] - cy + (rnd() - .5) * s * .3]);
	let R = 0;
	for (const p of loc) R = Math.max(R, Math.hypot(p[0], p[1]));
	return {
		own,
		node,
		files,
		s,
		region,
		loc,
		R,
		rho: R + .66 * s,
		cy0: 0
	};
}
function pkWidth(kW, lines, files) {
	const w = Math.max(.55, kW * Math.sqrt(lines + 40 * files));
	return CTX && CTX.quantise ? Math.exp(Math.round(Math.log(w) / .03) * .03) : w;
}
function pkFinishShape(S) {
	if (S.prims) {
		S.prims.sort((a, b) => a.d - b.d);
		let r = 0;
		for (const p of S.prims) r = Math.max(r, p.k === 0 ? Math.hypot(p.x, p.y) + p.rho : Math.max(Math.hypot(p.x0, p.y0), Math.hypot(p.x1, p.y1)) + p.hw);
		S.rad = r;
	}
	return S;
}
function emptyShape(kind) {
	return {
		kind,
		term: null,
		fork: null,
		efId: 0,
		prims: null,
		place: [],
		m: 0,
		area: 0,
		sx: 0,
		sy: 0,
		sxx: 0,
		syy: 0,
		sxy: 0,
		r0: 0,
		r1: 0,
		lines: 0,
		files: 0,
		w: 0,
		wb: 0,
		we: 0,
		rad: 0,
		grid: null,
		rc: null,
		sig: "",
		skey: ""
	};
}
function pkTermShape(t, kind, node) {
	const n = t.loc.length;
	const cy = -(t.R * .42 + t.s * .6);
	let sxx = 0, syy = 0, sy = 0, sxy = 0;
	for (const p of t.loc) {
		sxx += p[0] * p[0];
		syy += (cy + p[1]) * (cy + p[1]);
		sy += cy + p[1];
		sxy += p[0] * (cy + p[1]);
	}
	t.cy0 = cy;
	let lines = 0, files = 0;
	for (const f of t.files) if (!f.ghost) {
		lines += f.lines;
		files++;
	}
	const w = pkWidth(CTX.kW, lines, files);
	const S = emptyShape(kind);
	S.node = node;
	S.term = t;
	S.efId = -(1e6 + ++PK_FORK);
	const d0 = mkDisc(0, cy, t.rho, t);
	S.prims = [d0];
	S.m = Math.max(1, n);
	S.area = Math.PI * t.rho * t.rho;
	S.sy = sy;
	S.sxx = sxx;
	S.syy = syy;
	S.sxy = sxy;
	S.lines = lines;
	S.files = files;
	S.w = w;
	S.wb = w;
	S.we = w * .35;
	d0.d = pkDist(d0);
	S.skey = h2((kind === "own" ? "o" : "l") + ":" + node.path + ":" + t.region);
	S.sig = h2(S.skey + ":" + n + ":" + w.toFixed(4));
	CTX.sigs.push(S.sig);
	return pkFinishShape(S);
}
function pkLmin(S) {
	return S.kind === "virtual" ? 5 + S.w * .3 : S.term ? 6 + S.w * .5 : 8 + S.w * .6;
}
const PSI_H = [
	0,
	.25,
	.5,
	.75,
	1
];
const EXT_H = [
	0,
	.8,
	1.6
];
const PSI_L = [
	.12,
	.3,
	.48,
	.66,
	.84,
	1.02,
	1.2,
	1.38,
	1.56
];
function pkRange(S, pp) {
	let a = pp.rho + (pp.f > 0 ? S.r0 : -S.r1), b = pp.rho + (pp.f > 0 ? S.r1 : -S.r0);
	if (PK.stemR) {
		const psi = Math.atan2(pp.Ex, -pp.Ey);
		a = Math.min(a, psi);
		b = Math.max(b, psi);
	}
	return [a, b];
}
const leanOf = (S, psi, f) => {
	const r0 = f > 0 ? S.r0 : -S.r1, r1 = f > 0 ? S.r1 : -S.r0;
	return Math.max(-LEAN.lim - r0, Math.min(LEAN.lim - r1, LEAN.k * psi));
};
const flipFor = (S, side) => S.place.length && S.sx * side < 0 ? -1 : 1;
function pkUnion(owner, X, Y, fid, w) {
	const U = emptyShape("virtual");
	U.owner = owner;
	U.fork = fid;
	U.lines = X.lines + Y.lines;
	U.files = X.files + Y.files;
	U.w = w;
	U.wb = w;
	U.we = Math.min(w, Math.max(X.wb, Y.wb));
	U.m = X.m + Y.m;
	U.area = X.area + Y.area;
	U.r0 = 1e9;
	U.r1 = -1e9;
	return U;
}
function unionFromRec(owner, X, Y, fid, w, r, withPrims) {
	const U = pkUnion(owner, X, Y, fid, w);
	const pls = [[X, 0], [Y, 4]];
	if (withPrims) U.prims = [];
	for (const [S, o] of pls) {
		const pl = {
			S,
			Ex: r[o],
			Ey: r[o + 1],
			rho: r[o + 2],
			f: r[o + 3],
			c: stemCP(r[o], r[o + 1], r[o + 2])
		};
		U.place.push(pl);
		if (!withPrims) continue;
		const mo = pkMom(S, pl.rho, pl.f);
		for (const sg of pkStemSegs(S, pl.c, fid)) {
			sg.d = pkDist(sg);
			U.prims.push(sg);
		}
		for (const p of pkRotated(S, pl.rho, pl.f)) {
			const q = clonePrim(p, pl.Ex, pl.Ey);
			q.d = pkDist(q);
			U.prims.push(q);
		}
		const ex = pl.Ex, ey = pl.Ey;
		U.sx += mo.sx + S.m * ex;
		U.sy += mo.sy + S.m * ey;
		U.sxx += mo.sxx + 2 * ex * mo.sx + S.m * ex * ex;
		U.syy += mo.syy + 2 * ey * mo.sy + S.m * ey * ey;
		U.sxy += mo.sxy + ex * mo.sy + ey * mo.sx + S.m * ex * ey;
		const rr = pkRange(S, pl);
		U.r0 = Math.min(U.r0, rr[0]);
		U.r1 = Math.max(U.r1, rr[1]);
		S.grid = null;
		S.rc = null;
	}
	if (withPrims) pkFinishShape(U);
	return U;
}
function pkCombine(X, Y, owner, base, key) {
	const fid = ++PK_FORK;
	const w = pkWidth(CTX.kW, X.lines + Y.lines, X.files + Y.files);
	const sig = h2("(" + X.sig + "|" + Y.sig + "|" + w.toFixed(4) + (base ? "|B" + base[0].hw.toFixed(4) : "") + ")");
	const skey = key || h2("U(" + X.skey + "," + Y.skey + ")");
	if (CTX.cache) {
		const U = unionFromRec(owner, X, Y, fid, w, CTX.cache[CTX.ci++] || [
			0,
			-10,
			0,
			1,
			0,
			-10,
			0,
			1
		], false);
		U.sig = sig;
		U.skey = skey;
		CTX.unions.push(U);
		return U;
	}
	if (CTX.final) {
		const fv = CTX.final.get(skey);
		if (fv) {
			CTX.memoHits++;
			const U = unionFromRec(owner, X, Y, fid, w, fv.u, true);
			U.sig = sig;
			U.skey = skey;
			U.fromWarm = true;
			U.changed = fv.sig !== sig;
			CTX.unions.push(U);
			progress();
			return U;
		}
	}
	const memo = CTX.memo.get(sig);
	if (memo) {
		CTX.memoHits++;
		const U = unionFromRec(owner, X, Y, fid, w, memo, true);
		U.sig = sig;
		U.skey = skey;
		U.changed = !!CTX.final;
		CTX.unions.push(U);
		progress();
		return U;
	}
	const Lc = Math.max(22, w * 1.5);
	const basePrims = base ? base.map((p) => p.ef === -3 ? mkSeg(p.x0, p.y0, p.x1, p.y1, p.hw, p.sf, fid, p.term) : p) : [
		-.5,
		0,
		.5
	].map((a) => mkSeg(Math.sin(a) * Lc, Math.cos(a) * Lc, 0, 0, w * .5 + .8, -1, fid, null));
	const mkPhi = () => {
		const G = new Grid(64);
		for (const p of basePrims) pkAdd(G, p);
		return G;
	};
	const Phi0 = mkPhi();
	const H = X.m >= Y.m ? X : Y, L = H === X ? Y : X;
	const sH = H === X ? -1 : 1, sL = -sH;
	const fr = L.m / (H.m + L.m);
	for (const S of [H, L]) if (!S.grid && S.place.length) {
		S.grid = new Grid(S.prims.length * 4);
		for (const p of S.prims) pkAdd(S.grid, p);
	}
	const maxL = 3 * (H.rad + L.rad) + 300;
	const h = PK.heart * Math.sqrt((X.area + Y.area) / Math.PI) / .72;
	let best = null;
	const topLim = !!base && PK.rtop < 9 && !PK.stemR;
	let strict = true;
	const tryH = (aH, ext, lim) => {
		if (strict && topLim && aH > PK.rtop) return;
		const psiH = sH * aH, fH = flipFor(H, sH);
		const pH = pkSweep(H, psiH, leanOf(H, psiH, fH), fH, Phi0, pkLmin(H) + ext * L.rad, fid, lim, void 0, 0, h);
		if (!pH) return;
		const cH = pkCost(H, pH.mo, pH.Ex, pH.Ey, h);
		if (best && cH >= best.c) return;
		const Phi1 = mkPhi();
		for (const sg of pH.stem) pkAdd(Phi1, sg);
		for (const p of pkRotated(H, pH.rho, pH.f)) pkAdd(Phi1, clonePrim(p, pH.Ex, pH.Ey));
		const fL = flipFor(L, sL);
		for (const aL of PSI_L) {
			if (strict && topLim && aL > PK.rtop) continue;
			const psiL = sL * aL;
			const pL = pkSweep(L, psiL, leanOf(L, psiL, fL), fL, Phi1, pkLmin(L), fid, lim, best ? best.c : void 0, cH, h);
			if (!pL) continue;
			const c = cH + pkCost(L, pL.mo, pL.Ex, pL.Ey, h);
			if (strict && PK.stemR) {
				const rH = pkRange(H, pH), rL = pkRange(L, pL), lo = Math.min(rH[0], rL[0]), hi = Math.max(rH[1], rL[1]);
				if (hi - lo > 2 * LEAN.lim || base && (lo < -PK.rtop || hi > PK.rtop)) continue;
			}
			if (!best || c < best.c) best = {
				c,
				pH,
				pL
			};
		}
	};
	for (const ext of EXT_H) for (const aH of PSI_H) tryH(aH * (.35 + 1.3 * fr), ext, maxL);
	if (!best && PK.stemR) for (const ext of [2.4, 3.4]) for (const aH of PSI_H) tryH(aH * (.35 + 1.3 * fr), ext, maxL);
	strict = false;
	if (!best) for (const aH of [
		.3,
		.6,
		.9
	]) tryH(aH, .6, maxL);
	if (!best) for (const aH of [.5, .9]) tryH(aH, 1.2, 1e5);
	if (!best) {
		const far = 4 * (H.rad + L.rad) + 400;
		const r = [
			sH * far * .3,
			-far,
			0,
			1,
			sL * far * .3,
			-far,
			0,
			1
		];
		if (H !== X) r.splice(0, 8, r[4], r[5], r[6], r[7], r[0], r[1], r[2], r[3]);
		const U = unionFromRec(owner, X, Y, fid, w, r, true);
		U.sig = sig;
		U.skey = skey;
		U.changed = true;
		CTX.unions.push(U);
		return U;
	}
	const B = best;
	const pX = H === X ? B.pH : B.pL, pY = H === X ? B.pL : B.pH;
	const U = pkUnion(owner, X, Y, fid, w);
	U.prims = [];
	U.sig = sig;
	U.skey = skey;
	U.changed = !!CTX.final;
	for (const [S, pp] of [[X, pX], [Y, pY]]) {
		U.place.push({
			S,
			Ex: pp.Ex,
			Ey: pp.Ey,
			rho: pp.rho,
			f: pp.f,
			c: pp.c
		});
		for (const sg of pp.stem) {
			sg.d = pkDist(sg);
			U.prims.push(sg);
		}
		for (const p of pkRotated(S, pp.rho, pp.f)) {
			const q = clonePrim(p, pp.Ex, pp.Ey);
			q.d = pkDist(q);
			U.prims.push(q);
		}
		const mo = pp.mo, ex = pp.Ex, ey = pp.Ey;
		U.sx += mo.sx + S.m * ex;
		U.sy += mo.sy + S.m * ey;
		U.sxx += mo.sxx + 2 * ex * mo.sx + S.m * ex * ex;
		U.syy += mo.syy + 2 * ey * mo.sy + S.m * ey * ey;
		U.sxy += mo.sxy + ex * mo.sy + ey * mo.sx + S.m * ex * ey;
		const rr = pkRange(S, pp);
		U.r0 = Math.min(U.r0, rr[0]);
		U.r1 = Math.max(U.r1, rr[1]);
		S.grid = null;
		S.rc = null;
	}
	const rec = U.place.flatMap((p) => [
		p.Ex,
		p.Ey,
		p.rho,
		p.f
	]);
	CTX.unions.push(U);
	CTX.memo.set(sig, rec);
	progress();
	return pkFinishShape(U);
}
const PHASE_SEARCH = .45;
function progress() {
	CTX.work += 1;
	if (CTX.tick) CTX.tick(PHASE_SEARCH * Math.min(1, CTX.work / CTX.workTotal));
}
function splitAt(list, owner) {
	const tw = list.reduce((s, x) => s + x.m, 0);
	let acc = 0, k2 = 1, best = 0xde0b6b3a7640000;
	const cum = [];
	for (let i = 0; i < list.length - 1; i++) {
		acc += list[i].m;
		cum.push(acc);
		const dd = Math.abs(acc - tw / 2);
		if (dd < best) {
			best = dd;
			k2 = i + 1;
		}
	}
	const key = h2(owner.path + "|" + list[0].skey + "|" + list[list.length - 1].skey);
	const was = CTX.prevSplits ? CTX.prevSplits.get(key) : void 0;
	if (was !== void 0) {
		const j = list.findIndex((x) => x.skey === was);
		if (j >= 1 && Math.abs(cum[j - 1] - tw / 2) <= best + .12 * tw) k2 = j;
	}
	CTX.splits.set(key, list[k2].skey);
	return k2;
}
function pkFan(list, owner, base, key) {
	if (list.length === 1) return list[0];
	const k2 = splitAt(list, owner);
	const rk = (l) => h2("r:" + owner.path + "|" + l[0].skey + "|" + l[l.length - 1].skey);
	return pkCombine(pkFan(list.slice(0, k2), owner, void 0, rk(list.slice(0, k2))), pkFan(list.slice(k2), owner, void 0, rk(list.slice(k2))), owner, base, key || rk(list));
}
function pkNode(n, region, terms) {
	if (!n.kids.length) {
		const t = pkTerm(n, false, n.files, region);
		n.term = t;
		terms.push(t);
		return pkTermShape(t, "leaf", n);
	}
	const items = [];
	if (n.files.length) {
		const t = pkTerm(n, true, n.files, region);
		terms.push(t);
		items.push(pkTermShape(t, "own", n));
	}
	for (const k of n.kids) items.push(pkNode(k, region, terms));
	const S = items.length === 1 ? items[0] : pkFan(items, n, void 0, h2("n:" + n.path + ":" + region));
	if (S.kind === "virtual") {
		S.kind = "node";
		S.node = n;
		S.wb = S.w;
	}
	return S;
}
function pkLeaderSeq(items) {
	const byW = items.slice().sort((a, b) => b.m - a.m || cmpStr(a.node.name, b.node.name));
	if (PK.leaderOrder === "asc") return byW.slice().reverse();
	if (PK.leaderOrder === "desc") return byW;
	const lo = [], hi = [];
	byW.forEach((it, i) => (i % 2 ? hi : lo).push(it));
	return lo.reverse().concat(hi);
}
function pkLayout(root, region, order, trunkHW) {
	const terms = [];
	const items = [];
	if (root.files.length) {
		const t = pkTerm(root, true, root.files, region);
		terms.push(t);
		items.push(pkTermShape(t, "own", root));
	}
	for (const k of root.kids) items.push(pkNode(k, region, terms));
	if (!items.length) return {
		terms,
		top: null
	};
	let ord = order(items);
	const was = CTX.prevOrders ? CTX.prevOrders.get(region) : void 0;
	if (was) {
		const at = new Map(ord.map((x, i) => [x.skey, i]));
		const kept = was.map((k) => ord.find((x) => x.skey === k)).filter((x) => !!x);
		const keptSet = new Set(kept);
		const out = kept.slice();
		for (const x of ord) if (!keptSet.has(x)) out.splice(Math.min(out.length, at.get(x.skey)), 0, x);
		ord = out;
	}
	CTX.orders.set(region, ord.map((x) => x.skey));
	const base = [];
	for (let i = 0; i < 6; i++) base.push(mkSeg(0, i * 10 * 6, 0, (i + 1) * 10 * 6, trunkHW * (1 + .25 * i), -2, -3, null));
	let top = ord.length === 1 ? ord[0] : null;
	if (!top && PK.leader && ord.length > PK.leader) {
		const seq = pkLeaderSeq(ord);
		let U = seq[seq.length - 1];
		for (let i = seq.length - 2; i >= 0; i--) {
			const left = (seq.length - 2 - i) % 2 === 0, bse = i === 0 ? base : void 0;
			const k = h2("lead:" + region + ":" + i);
			U = left ? pkCombine(seq[i], U, root, bse, k) : pkCombine(U, seq[i], root, bse, k);
		}
		top = U;
		top.base = base;
	}
	if (!top) {
		const k2 = splitAt(ord, root);
		const rk = (l) => h2("r:" + region + "|" + l[0].skey + "|" + l[l.length - 1].skey);
		top = pkCombine(pkFan(ord.slice(0, k2), root, void 0, rk(ord.slice(0, k2))), pkFan(ord.slice(k2), root, void 0, rk(ord.slice(k2))), root, base, h2("top:" + region));
		top.base = base;
	}
	return {
		terms,
		top
	};
}
const mul = (A, B) => [
	A[0] * B[0] + A[1] * B[2],
	A[0] * B[1] + A[1] * B[3],
	A[2] * B[0] + A[3] * B[2],
	A[2] * B[1] + A[3] * B[3]
];
const app = (A, x, y) => [A[0] * x + A[1] * y, A[2] * x + A[3] * y];
function pkCompact(top, base, C, passes, region) {
	const recs = [];
	const G = new Grid(4096);
	for (const p of base) {
		const q = mkSeg(p.x0, p.y0, p.x1, p.y1, p.hw, p.sf, top.fork, p.term);
		q.sub = -5;
		pkAdd(G, q);
	}
	function mkStemWorld(rec, Ex, Ey, pool) {
		const pl = rec.pl, c = stemCP(Ex, Ey, pl.rho);
		const w = [];
		for (let i = 0; i < 8; i += 2) {
			const q = app(rec.A, c[i], c[i + 1]);
			w.push(rec.O[0] + q[0], rec.O[1] + q[1]);
		}
		return {
			c,
			segs: pkStemSegs(pl.S, w, rec.U.fork, pool)
		};
	}
	const discOf = (rec, S) => {
		const q = app(rec.Ac, 0, S.term.cy0);
		const d = mkDisc(rec.E[0] + q[0], rec.E[1] + q[1], S.term.rho, S.term);
		d.sub = rec.lo;
		d.m = S.m;
		return d;
	};
	(function walk(U, O, A, depth) {
		for (const pl of U.place) {
			const rec = {
				U,
				pl,
				O,
				A,
				Ac: A,
				E: [0, 0],
				lo: recs.length,
				hi: 0,
				depth,
				own: []
			};
			recs.push(rec);
			for (const sg of mkStemWorld(rec, pl.Ex, pl.Ey).segs) {
				sg.sub = rec.lo;
				rec.own.push(sg);
				pkAdd(G, sg);
			}
			const e = app(A, pl.Ex, pl.Ey);
			rec.E = [O[0] + e[0], O[1] + e[1]];
			const cr = Math.cos(pl.rho), sr = Math.sin(pl.rho), f = pl.f || 1;
			rec.Ac = mul(A, [
				cr * f,
				-sr,
				sr * f,
				cr
			]);
			if (pl.S.term) {
				const d = discOf(rec, pl.S);
				rec.own.push(d);
				pkAdd(G, d);
			} else walk(pl.S, rec.E, rec.Ac, depth + 1);
			rec.hi = recs.length;
		}
	})(top, [0, 0], [
		1,
		0,
		0,
		1
	], 0);
	const order = recs.map((_r, i) => i).sort((a, b) => recs[a].depth - recs[b].depth || recs[b].hi - recs[b].lo - (recs[a].hi - recs[a].lo));
	const prev = CTX.prevSigs;
	const totalPasses = passes;
	const changes = [];
	const evalAt = new Int32Array(recs.length).fill(-1);
	const reach = new Float64Array(recs.length * 4);
	const REACH_PAD = 3 * PK.CS + 8;
	const logChange = (b) => changes.push(b[0], b[1], b[2], b[3]);
	let deadN = 0;
	let moved = 0;
	const evalRec = (ri, mode) => {
		const rec = recs[ri], pl = rec.pl, S = pl.S;
		if (mode === 0 && evalAt[ri] >= 0) {
			let dirty = false;
			const r0 = ri * 4;
			for (let c = evalAt[ri] * 4; c < changes.length && !dirty; c += 4) dirty = changes[c] <= reach[r0 + 2] && changes[c + 2] >= reach[r0] && changes[c + 1] <= reach[r0 + 3] && changes[c + 3] >= reach[r0 + 1];
			if (!dirty) return true;
		}
		const sub = [];
		let m = 0, sx = 0, sy = 0;
		let bx0 = 0xde0b6b3a7640000, by0 = 0xde0b6b3a7640000, bx1 = -0xde0b6b3a7640000, by1 = -0xde0b6b3a7640000;
		for (let k = rec.lo; k < rec.hi; k++) for (const p of recs[k].own) {
			if (k === rec.lo && p.k === 1) continue;
			sub.push(p);
			if (p.k === 0) {
				m += p.m;
				sx += p.m * (p.x - C[0]);
				sy += p.m * (p.y - C[1]);
			}
			if (p.bx0 < bx0) bx0 = p.bx0;
			if (p.by0 < by0) by0 = p.by0;
			if (p.bx1 > bx1) bx1 = p.bx1;
			if (p.by1 > by1) by1 = p.by1;
		}
		if (!m) return true;
		const own = new Occ(sub.length * 2 + 8);
		for (let k = rec.lo; k < rec.hi; k++) for (const p of recs[k].own) occAdd(own, p, 1);
		const ox = rec.O[0], oy = rec.O[1];
		const dO = (p) => p.k === 0 ? Math.hypot(p.x - ox, p.y - oy) - p.rho : Math.min(Math.hypot(p.x0 - ox, p.y0 - oy), Math.hypot(p.x1 - ox, p.y1 - oy));
		const dk = new Float64Array(sub.length);
		for (let i = 0; i < sub.length; i++) dk[i] = dO(sub[i]);
		const subS = Array.from(sub.keys()).sort((a, b) => dk[a] - dk[b]).map((i) => sub[i]);
		const groups = bucketsOf(subS);
		const cost = (dx, dy) => PK.cwx * (2 * dx * sx + m * dx * dx) + PK.cwy * (2 * dy * sy + m * dy * dy);
		const psi0 = Math.atan2(pl.Ex, -pl.Ey), lmin = pkLmin(S), l0 = Math.hypot(pl.Ex, pl.Ey);
		const lmax = Math.max(l0 * 1.3, l0 + 30);
		let best = null;
		let rdx0 = 0, rdx1 = 0, rdy0 = 0, rdy1 = 0, sbx0 = 0xde0b6b3a7640000, sby0 = 0xde0b6b3a7640000, sbx1 = -0xde0b6b3a7640000, sby1 = -0xde0b6b3a7640000;
		const test = (Ex, Ey, dx, dy) => {
			const st = mkStemWorld(rec, Ex, Ey, COMPACT_POOL);
			if (dx < rdx0) rdx0 = dx;
			if (dx > rdx1) rdx1 = dx;
			if (dy < rdy0) rdy0 = dy;
			if (dy > rdy1) rdy1 = dy;
			for (const g of st.segs) {
				if (g.bx0 < sbx0) sbx0 = g.bx0;
				if (g.by0 < sby0) sby0 = g.by0;
				if (g.bx1 > sbx1) sbx1 = g.bx1;
				if (g.by1 > sby1) sby1 = g.by1;
				if (g.bx0 - dx < sbx0) sbx0 = g.bx0 - dx;
				if (g.by0 - dy < sby0) sby0 = g.by0 - dy;
				if (g.bx1 - dx > sbx1) sbx1 = g.bx1 - dx;
				if (g.by1 - dy > sby1) sby1 = g.by1 - dy;
			}
			let pen = 0, frac = 1, self = false;
			PK_IG0 = rec.lo;
			PK_IG1 = rec.hi;
			PK_ONLY = false;
			OWN = own;
			for (let i = 0; i < st.segs.length && !pen; i++) {
				pen = pkHit(G, st.segs[i], 0, 0);
				if (pen) frac = Math.max(.35, (i + .5) / st.segs.length);
			}
			if (!pen) {
				PK_ONLY = true;
				for (let i = 0; i < st.segs.length && !pen; i++) {
					pen = pkHit(G, st.segs[i], -dx, -dy);
					if (pen) self = true;
				}
				PK_ONLY = false;
			}
			if (!pen) pen = hitGroups(G, subS, groups, dx, dy);
			PK_IG0 = PK_IG1 = -1;
			OWN = null;
			return {
				pen,
				frac,
				self,
				st
			};
		};
		if (mode !== 0) {
			if (!test(pl.Ex, pl.Ey, 0, 0).pen) return true;
			if (mode === 2) return false;
			for (const da of [
				0,
				.08,
				-.08,
				.16,
				-.16,
				.3,
				-.3,
				.5,
				-.5
			]) {
				const ux = Math.sin(psi0 + da), uy = -Math.cos(psi0 + da);
				const uw = app(rec.A, ux, uy);
				let l = l0;
				for (let it = 0; it < 80 && l <= l0 * 2.5 + 300; it++) {
					const Ex = ux * l, Ey = uy * l;
					const ew = app(rec.A, Ex, Ey);
					const dx = rec.O[0] + ew[0] - rec.E[0], dy = rec.O[1] + ew[1] - rec.E[1];
					const r = test(Ex, Ey, dx, dy);
					if (!r.pen) {
						best = {
							cc: 0,
							Ex,
							Ey,
							dx,
							dy,
							st: {
								c: r.st.c,
								segs: r.st.segs.map((q) => clonePrim(q, 0, 0))
							}
						};
						break;
					}
					const dot = PKC.x * uw[0] + PKC.y * uw[1], dd = PKC.x * PKC.x + PKC.y * PKC.y;
					const step = (-dot + Math.sqrt(Math.max(0, dot * dot - dd + PKC.need * PKC.need))) / r.frac;
					l += Math.max(.6, Math.min(step + .15, 400), l * .015);
				}
				if (best) break;
			}
			if (!best) return false;
			CTX.resolved++;
		} else {
			const cands = [psi0];
			for (let a = -1.56; a <= 1.57; a += PK.cstep) if (Math.abs(a - psi0) > .03) cands.push(a);
			const wAng = (v) => Math.abs(Math.atan2(v[0], -v[1]));
			const wa0 = wAng(app(rec.A, Math.sin(psi0), -Math.cos(psi0)));
			for (const psi of cands) {
				const ux = Math.sin(psi), uy = -Math.cos(psi);
				const uw = app(rec.A, ux, uy);
				if (wAng(uw) > Math.max(PK.cmax, wa0) + 1e-9) continue;
				let l = lmin;
				for (let it = 0; it < 120 && l <= lmax; it++) {
					const Ex = ux * l, Ey = uy * l;
					const ew = app(rec.A, Ex, Ey);
					const dx = rec.O[0] + ew[0] - rec.E[0], dy = rec.O[1] + ew[1] - rec.E[1];
					const cc = cost(dx, dy);
					if (cc >= (best ? best.cc : -1e-6)) break;
					const r = test(Ex, Ey, dx, dy);
					if (!r.pen) {
						best = {
							cc,
							Ex,
							Ey,
							dx,
							dy,
							st: {
								c: r.st.c,
								segs: r.st.segs.map((q) => clonePrim(q, 0, 0))
							}
						};
						break;
					}
					if (r.self) break;
					const dot = PKC.x * uw[0] + PKC.y * uw[1], dd = PKC.x * PKC.x + PKC.y * PKC.y;
					const step = (-dot + Math.sqrt(Math.max(0, dot * dot - dd + PKC.need * PKC.need))) / r.frac;
					l += Math.max(.6, Math.min(step + .15, 400), l * .015);
				}
			}
		}
		if (!best) {
			{
				evalAt[ri] = changes.length / 4;
				const r0 = ri * 4;
				reach[r0] = Math.min(bx0 + rdx0, sbx0) - REACH_PAD;
				reach[r0 + 1] = Math.min(by0 + rdy0, sby0) - REACH_PAD;
				reach[r0 + 2] = Math.max(bx1 + rdx1, sbx1) + REACH_PAD;
				reach[r0 + 3] = Math.max(by1 + rdy1, sby1) + REACH_PAD;
			}
			return true;
		}
		evalAt[ri] = -1;
		moved++;
		const chg = [
			0xde0b6b3a7640000,
			0xde0b6b3a7640000,
			-0xde0b6b3a7640000,
			-0xde0b6b3a7640000
		];
		const grow = (p) => {
			if (p.bx0 < chg[0]) chg[0] = p.bx0;
			if (p.by0 < chg[1]) chg[1] = p.by0;
			if (p.bx1 > chg[2]) chg[2] = p.bx1;
			if (p.by1 > chg[3]) chg[3] = p.by1;
		};
		for (let k = rec.lo; k < rec.hi; k++) for (const p of recs[k].own) grow(p);
		pl.Ex = best.Ex;
		pl.Ey = best.Ey;
		pl.c = best.st.c;
		for (const p of rec.own) {
			p.dead = true;
			occAdd(G.occ, p, -1);
			deadN++;
		}
		rec.own = [];
		for (const sg of best.st.segs) {
			sg.sub = rec.lo;
			rec.own.push(sg);
			pkAdd(G, sg);
		}
		for (let k = rec.lo; k < rec.hi; k++) {
			const r = recs[k];
			if (k > rec.lo) {
				r.O = [r.O[0] + best.dx, r.O[1] + best.dy];
				const nw = [];
				for (const p of r.own) {
					p.dead = true;
					occAdd(G.occ, p, -1);
					deadN++;
					const q = clonePrim(p, best.dx, best.dy);
					q.sub = k;
					q.m = p.m;
					nw.push(q);
					pkAdd(G, q);
				}
				r.own = nw;
			}
			r.E = [r.E[0] + best.dx, r.E[1] + best.dy];
		}
		if (S.term) {
			const d = discOf(rec, S);
			rec.own.push(d);
			pkAdd(G, d);
		}
		for (let k = rec.lo; k < rec.hi; k++) for (const p of recs[k].own) grow(p);
		logChange(chg);
		if (deadN > 2048) {
			G.purge();
			deadN = 0;
		}
		return true;
	};
	if (prev) {
		const parentOf = new Int32Array(recs.length).fill(-1);
		for (let i = 0; i < recs.length; i++) for (let k = recs[i].lo + 1; k < recs[i].hi; k++) if (recs[k].depth === recs[i].depth + 1) parentOf[k] = i;
		const deep = order.slice().reverse();
		let must = /* @__PURE__ */ new Set();
		for (let i = 0; i < recs.length; i++) if (!prev.has(recs[i].pl.S.sig)) must.add(i);
		for (let round = 0; round < 8 && must.size; round++) {
			const next = /* @__PURE__ */ new Set();
			for (const ri of deep) {
				if (!must.has(ri)) continue;
				if (!evalRec(ri, 1) && parentOf[ri] >= 0) next.add(parentOf[ri]);
			}
			must = next;
		}
		for (let i = 0; i < recs.length && !CTX.warmFailed; i++) if (!evalRec(i, 2)) CTX.warmFailed = true;
	}
	for (let pass = 0; pass < totalPasses; pass++) {
		moved = 0;
		for (const ri of order) evalRec(ri, 0);
		(CTX.passLog || (CTX.passLog = [])).push([
			region,
			pass,
			moved,
			recs.length
		]);
		if (CTX.tick) CTX.tick(PHASE_SEARCH + .55 * ((pass + 1) / totalPasses) * (region === "c" ? .92 : 1));
	}
}
function hitGroups(G, P, B, dx, dy) {
	CTX.stats.hits++;
	const g = ++B.gen * 2, of = B.of, flag = B.flag, box = B.box;
	for (let i = 0; i < P.length; i++) {
		const b = of[i];
		let f = flag[b];
		if (f < g) {
			const o = b * 4;
			f = flag[b] = g + (occMayBox(G, box[o] + dx, box[o + 1] + dy, box[o + 2] + dx, box[o + 3] + dy) ? 1 : 0);
		}
		if (f === g) continue;
		const v = pkHit(G, P[i], dx, dy);
		if (v) return v;
	}
	return 0;
}
function pkEmit(out, S, T, map, region) {
	const tp = (x, y) => map(T.ox + T.a * x + T.b * y, T.oy + T.c * x + T.d * y);
	if (S.term) {
		const t = S.term;
		t.leaves = t.loc.map((p, i) => {
			const q = tp(p[0], t.cy0 + p[1]);
			return {
				x: q[0],
				y: q[1],
				file: t.files[i]
			};
		});
		return;
	}
	const own = S.kind === "node" ? S.node : S.owner;
	for (const pl of S.place) {
		const C = pl.S, c = pl.c;
		const p0 = tp(c[0], c[1]), p1 = tp(c[2], c[3]), p2 = tp(c[4], c[5]), p3 = tp(c[6], c[7]);
		const cp = [
			p0[0],
			p0[1],
			p1[0],
			p1[1],
			p2[0],
			p2[1],
			p3[0],
			p3[1]
		];
		const ex = cp[6], ey = cp[7];
		const node = C.kind === "node" || C.kind === "leaf" ? C.node : null;
		const b = {
			x0: cp[0],
			y0: cp[1],
			x1: ex,
			y1: ey,
			cp,
			w: C.w,
			wb: C.wb,
			we: C.we,
			node,
			owner: node ? node.parent : C.kind === "own" ? C.node : C.owner || null,
			virtual: C.kind === "virtual",
			own: C.kind === "own",
			term: C.term || null,
			region,
			depth: (own ? own.depth : 0) + 1,
			id: out.length
		};
		out.push(b);
		if (node) {
			node.F = [ex, ey];
			node.branch = b;
			node.w = C.w;
		}
		if (C.term) {
			C.term.F = [ex, ey];
			C.term.branch = b;
		}
		const cr = Math.cos(pl.rho), sr = Math.sin(pl.rho), f = pl.f || 1;
		const b00 = cr * f, b01 = -sr, b10 = sr * f, b11 = cr;
		const ox = T.ox + T.a * pl.Ex + T.b * pl.Ey, oy = T.oy + T.c * pl.Ex + T.d * pl.Ey;
		pkEmit(out, C, {
			a: T.a * b00 + T.b * b10,
			b: T.a * b01 + T.b * b11,
			c: T.c * b00 + T.d * b10,
			d: T.c * b01 + T.d * b11,
			ox,
			oy
		}, map, region);
	}
}
function pkEmitTop(out, S, owner, map, region) {
	const T = {
		a: 1,
		b: 0,
		c: 0,
		d: 1,
		ox: 0,
		oy: 0
	};
	if (S.kind === "virtual") return pkEmit(out, S, T, map, region);
	const wrap = emptyShape("virtual");
	wrap.owner = owner;
	wrap.place = [{
		S,
		Ex: 0,
		Ey: -20,
		rho: 0,
		f: 1,
		c: stemCP(0, -20, 0)
	}];
	pkEmit(out, wrap, T, map, region);
}
//#endregion
//#region src/lib/codetree/model.ts
const dirOf = (p) => {
	const i = p.lastIndexOf("/");
	return i < 0 ? "" : p.slice(0, i);
};
const baseOf = (p) => p.slice(p.lastIndexOf("/") + 1);
function shortName(name) {
	if (name.length <= 24 || !name.includes("/")) return name;
	const s = name.split("/");
	return s[0] + "/…/" + s[s.length - 1];
}
function mkNode(name, path, parent, kind) {
	return {
		name,
		path,
		parent,
		kids: [],
		files: [],
		depth: parent ? parent.depth + 1 : 0,
		kind,
		nFiles: 0,
		nGhost: 0,
		lines: 0,
		id: -1,
		limb: 0,
		bx0: 0,
		by0: 0,
		bx1: 0,
		by1: 0,
		sx: 0,
		sy: 0,
		cnt: 0,
		cx: 0,
		cy: 0,
		rad: 0
	};
}
function* allNodesGen(n) {
	yield n;
	for (const k of n.kids) yield* allNodesGen(k);
}
function allNodes(n) {
	return [...allNodesGen(n)];
}
function parseFiles(raw) {
	const F = raw.files.map((f, i) => ({
		id: i,
		path: f.p,
		name: baseOf(f.p),
		lines: f.n,
		kind: f.k,
		test: !!f.t,
		imports: f.i || [],
		usedBy: [],
		target: f.g,
		ghost: false,
		place: "crown",
		node: null,
		leaf: null,
		tests: null
	}));
	for (const f of F) for (const j of f.imports) if (F[j]) F[j].usedBy.push(f.id);
	const top = {};
	for (const f of F) {
		if (f.test || !f.path.includes("/")) continue;
		const k = f.path.split("/")[0];
		const s = top[k] || (top[k] = {
			n: 0,
			c: 0
		});
		s.n += f.lines + 1;
		if (f.kind === "c") s.c += f.lines + 1;
	}
	for (const f of F) if (f.test && f.kind === "c") f.place = "root";
	else if (f.test) f.place = "ground";
	else if (!f.path.includes("/")) f.place = f.kind === "c" ? "crown" : "ground";
	else {
		const s = top[f.path.split("/")[0]];
		f.place = s.c / s.n < .3 ? "ground" : "crown";
	}
	return F;
}
function buildTree(rootName, entries, kind) {
	const root = mkNode(rootName, "", null, kind);
	const byPath = /* @__PURE__ */ new Map([["", root]]);
	for (const { segs, file } of entries) {
		let n = root, p = "";
		for (const s of segs) {
			p = p ? p + "/" + s : s;
			let c = byPath.get(p);
			if (!c) {
				c = mkNode(s, p, n, kind);
				n.kids.push(c);
				byPath.set(p, c);
			}
			n = c;
		}
		n.files.push(file);
	}
	const alias = /* @__PURE__ */ new Map();
	(function compact(n) {
		for (const k of n.kids) {
			while (k.files.length === 0 && k.kids.length === 1) {
				const c = k.kids[0];
				alias.set(k.path, c.path);
				k.name += "/" + c.name;
				k.path = c.path;
				k.kids = c.kids;
				k.files = c.files;
				for (const g of k.kids) g.parent = k;
			}
			compact(k);
		}
	})(root);
	const nodeOf = /* @__PURE__ */ new Map();
	(function fin(n, d) {
		n.depth = d;
		nodeOf.set(n.path, n);
		n.kids.sort((a, b) => cmpStr(a.name, b.name));
		n.files.sort((a, b) => b.lines - a.lines || cmpStr(a.name, b.name));
		n.nFiles = 0;
		n.nGhost = 0;
		n.lines = 0;
		for (const f of n.files) if (f.ghost) n.nGhost++;
		else {
			n.nFiles++;
			n.lines += f.lines;
		}
		for (const k of n.kids) {
			fin(k, d + 1);
			n.nFiles += k.nFiles;
			n.nGhost += k.nGhost;
			n.lines += k.lines;
		}
	})(root, 0);
	for (const [a] of alias) {
		let p = a;
		while (alias.has(p)) p = alias.get(p);
		if (nodeOf.has(p)) nodeOf.set(a, nodeOf.get(p));
	}
	return {
		root,
		nodeOf
	};
}
const qlog = (v) => v > 0 ? Math.exp(Math.round(Math.log(v) / .03) * .03) : v;
function dataSig(raw) {
	let h = 2166136261;
	const mix = (s) => {
		for (let i = 0; i < s.length; i++) {
			h ^= s.charCodeAt(i);
			h = Math.imul(h, 16777619);
		}
	};
	mix(raw.name);
	for (const f of raw.files) mix("\0" + f.p + "" + f.n + "" + f.k + (f.t ? "t" : "") + "" + (f.g ?? ""));
	return raw.files.length + ":" + (h >>> 0).toString(36);
}
function mixAng(a, b, t) {
	let d = b - a;
	while (d > Math.PI) d -= TAU;
	while (d < -Math.PI) d += TAU;
	return a + d * t;
}
function buildModel(raw, opts = {}) {
	let replay = opts.replay && opts.replay.v === 1 ? opts.replay : null;
	if (replay && replay.files !== raw.files.length) replay = null;
	const warm = !replay && opts.warm && opts.warm.v === 1 ? opts.warm : null;
	const F = parseFiles(raw);
	const crownEntries = [];
	const groundGroups = /* @__PURE__ */ new Map();
	for (const f of F) if (f.place === "crown") crownEntries.push({
		segs: dirOf(f.path) ? dirOf(f.path).split("/") : [],
		file: f
	});
	else if (f.place === "ground") {
		const k = f.path.includes("/") ? f.path.split("/")[0] : "(repo root)";
		if (!groundGroups.has(k)) groundGroups.set(k, []);
		groundGroups.get(k).push(f);
	}
	const crown = buildTree(raw.name, crownEntries, "crown");
	const M = {
		name: raw.name,
		files: F,
		crown: crown.root,
		nodeOf: crown.nodeOf,
		branches: [],
		leaves: [],
		terms: []
	};
	for (const n of allNodes(M.crown)) for (const f of n.files) f.node = n;
	const nCrown = crownEntries.length;
	const Rest = Math.sqrt(Math.max(1, nCrown) * 10 * 10 / 1.6) * 1.15;
	const allLines = M.crown.lines + 40 * M.crown.nFiles;
	M.kW = Rest * .05 / Math.sqrt(Math.max(1, allLines));
	if (opts.quantise) M.kW = qlog(M.kW);
	const widthOf = (lines, files) => Math.max(.55, M.kW * Math.sqrt(lines + 40 * files));
	M.trunkW = widthOf(M.crown.lines, M.crown.nFiles);
	const ctx = newCtx(M.kW);
	ctx.quantise = !!opts.quantise;
	if (replay) {
		ctx.cache = replay.u;
		ctx.prevSplits = new Map(replay.splits || []);
		ctx.prevOrders = new Map(replay.orders || []);
	}
	if (warm) {
		ctx.memo = new Map(warm.memo);
		ctx.final = new Map(warm.final.map(([k, u, sg]) => [k, {
			u,
			sig: sg
		}]));
		ctx.prevSigs = new Set(warm.sigs);
		ctx.prevSplits = new Map(warm.splits);
		ctx.prevOrders = new Map(warm.orders);
	}
	ctx.passes = opts.passes ?? (warm ? 0 : PK.compact);
	ctx.tick = opts.tick;
	let clumps = 0;
	for (const n of allNodes(M.crown)) if (n.files.length) clumps++;
	ctx.workTotal = Math.max(1, clumps + Math.ceil(F.filter((f) => f.place === "root").length / 6));
	beginBuild(ctx);
	const CR = nCrown >= CROWN_BIG.min ? Object.assign({}, CROWN, CROWN_BIG) : CROWN;
	pkUse(CR);
	const LC = pkLayout(M.crown, "crown", (items) => {
		const byW = items.slice().sort((a, b) => b.m - a.m || cmpStr(a.node.name, b.node.name));
		const L = [], R = [];
		byW.forEach((it, i) => (i % 2 ? R : L).push(it));
		return L.reverse().concat(R);
	}, M.trunkW * .62);
	ctx.phase = 1;
	const tCompact0 = typeof performance !== "undefined" ? performance.now() : 0;
	if (LC.top && LC.top.base && !ctx.cache) pkCompact(LC.top, LC.top.base, [0, -PK.heart * (CR.cheart || 1) * Math.sqrt(LC.top.area / Math.PI) / .72], ctx.passes, "c");
	const tCompact = (typeof performance !== "undefined" ? performance.now() : 0) - tCompact0;
	const emitted = M.branches;
	if (LC.top) {
		M.crown.F = [0, 0];
		M.crown.w = M.trunkW;
		pkEmitTop(emitted, LC.top, M.crown, (x, y) => [x, y], "crown");
	}
	const limbOfPath = (p) => {
		if (p == null) return null;
		let q = typeof p === "number" ? dirOf(F[p].path) : p;
		while (q && !M.nodeOf.has(q)) q = dirOf(q);
		if (!q) return null;
		let n = M.nodeOf.get(q);
		while (n && n.depth > 2) n = n.parent;
		return n && n.depth >= 1 ? n : null;
	};
	const rootEntriesArr = [];
	for (const f of F) {
		if (f.place !== "root") continue;
		const n = limbOfPath(f.target);
		f.tests = n;
		let segs;
		if (n) {
			const chain = [];
			let q = n;
			while (q && q.depth >= 1) {
				chain.unshift(q.name);
				q = q.parent;
			}
			segs = chain.map((nm, i) => (i === 0 ? "T:" : "") + nm);
		} else {
			const d = dirOf(f.path).split("/");
			const lm = M.nodeOf.get(d[0]);
			if (lm && lm.depth === 1) {
				f.tests = lm;
				segs = ["T:" + lm.name];
			} else segs = ["U:" + d[0]].concat(d.length > 1 ? [d[1]] : []);
		}
		rootEntriesArr.push({
			segs,
			file: f
		});
	}
	const roots = buildTree("tests", rootEntriesArr, "root");
	M.roots = roots.root;
	M.rootNodeOf = roots.nodeOf;
	for (const n of allNodes(M.roots)) {
		if (n.depth === 0) continue;
		n.unmatched = n.path.split("/")[0].startsWith("U:");
		const crownPath = n.path.split("/").map((s) => s.replace(/^[TU]:/, "")).join("/");
		n.crownTwin = n.unmatched ? null : M.nodeOf.get(crownPath) || null;
		n.name = n.name.replace(/^[TU]:/, "");
		n.label = n.unmatched ? "tests: " + shortName(crownPath) : "tests for " + shortName(n.crownTwin ? n.crownTwin.path : n.name);
		for (const f of n.files) f.node = n;
	}
	const crownX = /* @__PURE__ */ new Map();
	for (const t of LC.terms) for (const l of t.leaves || []) {
		let n = t.node;
		while (n && n.depth > 1) n = n.parent;
		if (!n || n.depth < 1) continue;
		const e = crownX.get(n) || [0, 0];
		e[0] += l.x;
		e[1]++;
		crownX.set(n, e);
	}
	pkUse(ROOTS);
	const LR = pkLayout(M.roots, "root", (items) => items.slice().sort((A2, B2) => {
		const x = (it) => {
			const nd = it.node;
			if (it.kind === "own" || nd.unmatched) return 0;
			let q = nd.crownTwin || null;
			while (q && q.depth > 1) q = q.parent;
			const e = q ? crownX.get(q) : void 0;
			return e ? e[0] / e[1] : 0;
		};
		return x(A2) - x(B2) || cmpStr(A2.node.name, B2.node.name);
	}), M.trunkW * .62);
	if (LR.top && LR.top.base && !ctx.cache) pkCompact(LR.top, LR.top.base, [0, -PK.heart * Math.sqrt(LR.top.area / Math.PI) / .72], warm ? 0 : 1, "r");
	if (ctx.warmFailed) {
		const M2 = buildModel(raw, {
			...opts,
			warm: null,
			replay: null
		});
		M2.buildStats.warmFallback = true;
		return M2;
	}
	let maxY = 0, minX = 0, maxX = 0, minY = 0;
	for (const t of LC.terms) for (const l of t.leaves || []) {
		maxY = Math.max(maxY, l.y);
		minX = Math.min(minX, l.x);
		maxX = Math.max(maxX, l.x);
		minY = Math.min(minY, l.y);
	}
	M.Rtyp = Math.max((maxX - minX) / 2, -minY) * .8;
	const groundY = Math.max(M.Rtyp * (CR.trunk || .42), maxY + 40, M.trunkW * 2.5);
	M.groundY = groundY;
	if (LR.top) {
		const ry = groundY + 16;
		M.roots.F = [0, ry];
		M.roots.w = widthOf(M.roots.lines, M.roots.nFiles) * .8;
		const n0 = M.branches.length;
		pkEmitTop(emitted, LR.top, M.roots, (x, y) => [x, ry - y], "root");
		for (let i = n0; i < M.branches.length; i++) {
			const b = M.branches[i];
			b.w *= .9;
			b.wb *= .9;
			b.we *= .9;
		}
	}
	M.piles = [];
	const gg = [...groundGroups.entries()].sort((a, b) => b[1].length - a[1].length || cmpStr(a[0], b[0]));
	let xl = -90 - M.trunkW, xr = 90 + M.trunkW;
	gg.forEach(([k, fs], i) => {
		fs.sort((a, b) => b.lines - a.lines || cmpStr(a.name, b.name));
		const n = fs.length, s = 9.5;
		const W = Math.max(30, Math.sqrt(n) * s * 2.1), H = Math.max(12, W * .28);
		const left = i % 2 === 0;
		const cx = left ? xl - W / 2 : xr + W / 2;
		if (left) xl -= W + 50;
		else xr += W + 50;
		const pts = [];
		const rnd = rng(hashStr(k));
		for (let y = 0; pts.length < n * 3 && y < H * 3; y += s * .62) for (let x = -W; x <= W; x += s * .95) {
			const xx = x + (y / (s * .62) % 2 ? s * .45 : 0);
			const e = xx * xx / (W * W / 4) + y * y / (H * H);
			pts.push([
				xx + (rnd() - .5) * s * .3,
				y,
				e
			]);
		}
		pts.sort((a, b) => a[2] - b[2]);
		const node = mkNode(k, "~" + k, null, "pile");
		node.label = k === "(repo root)" ? "repo root files" : k;
		node.files = fs;
		node.nFiles = n;
		node.lines = fs.reduce((a, f) => a + f.lines, 0);
		node.depth = 1;
		const leaves = pts.slice(0, n).map((p, j) => ({
			x: cx + p[0],
			y: groundY - p[1] - 3.5,
			file: fs[j],
			flat: true,
			rot: (rnd() - .5) * 1.2
		}));
		for (const f of fs) f.node = node;
		node.cx = cx;
		node.pw = W;
		node.h = H;
		node.leaves = leaves;
		M.piles.push(node);
	});
	const finishTerms = (terms, region) => {
		for (const t0 of terms) {
			const t = t0;
			t.region = region;
			let cx = 0, cy = 0;
			for (const l of t.leaves) {
				cx += l.x;
				cy += l.y;
			}
			t.cx = cx / Math.max(1, t.leaves.length);
			t.cy = cy / Math.max(1, t.leaves.length);
			t.rad = 0;
			for (const l of t.leaves) t.rad = Math.max(t.rad, Math.hypot(l.x - t.cx, l.y - t.cy));
			t.rad += t.s * .5;
			M.terms.push(t);
		}
	};
	finishTerms(LC.terms, "crown");
	finishTerms(LR.terms, "root");
	for (const t of M.terms) {
		const tb = t.branch;
		const bx = tb ? tb.x1 : t.cx, by = tb ? tb.y1 : t.cy;
		const r2 = rng(hashStr(t.node.path + "@"));
		const hb = tb ? Math.atan2(tb.cp[7] - tb.cp[5], tb.cp[6] - tb.cp[4]) : -Math.PI / 2;
		for (const l of t.leaves) {
			const f = l.file;
			const fz = Math.min(1, Math.log10(Math.max(1, f.lines) + 1) / 3.6);
			l.len = Math.min(t.s * 1.12, 10 * (.5 + .62 * fz)) * (t.region === "root" ? .55 : 1);
			const radial = Math.atan2(l.y - by, l.x - bx);
			l.ang = (Math.hypot(l.y - by, l.x - bx) < t.s * .6 ? hb : mixAng(radial, hb, .4)) + (r2() - .5) * .7;
			l.term = t;
			l.region = t.region;
			l.shade = r2() < .5 ? 0 : 1;
			f.leaf = l;
			l.id = M.leaves.length;
			M.leaves.push(l);
		}
	}
	for (const p of M.piles) for (const l of p.leaves) {
		const f = l.file;
		l.len = 10 * (.55 + .5 * Math.min(1, Math.log10(Math.max(1, f.lines) + 1) / 3.6));
		l.ang = l.rot;
		l.region = "ground";
		l.pile = p;
		l.shade = 0;
		f.leaf = l;
		l.id = M.leaves.length;
		M.leaves.push(l);
	}
	M.nodes = [];
	for (const R of [M.crown, M.roots]) for (const n of allNodes(R)) {
		n.id = M.nodes.length;
		M.nodes.push(n);
	}
	for (const p of M.piles) {
		p.id = M.nodes.length;
		M.nodes.push(p);
		p.F = [p.cx, groundY - p.h * .5];
	}
	let limbI = 0;
	for (const n of M.crown.kids.slice().sort((a, b) => (a.F ? a.F[0] : 0) - (b.F ? b.F[0] : 0))) n.limb = limbI++;
	M.limbCount = limbI;
	let rl = 0;
	for (const n of M.roots.kids) n.limb = rl++;
	for (const n of M.nodes) if (n.depth > 1) {
		let q = n;
		while (q && q.depth > 1) q = q.parent;
		n.limb = q && q.limb !== void 0 ? q.limb : 0;
	}
	for (const n of M.nodes) {
		n.bx0 = 1e9;
		n.by0 = 1e9;
		n.bx1 = -1e9;
		n.by1 = -1e9;
		n.sx = 0;
		n.sy = 0;
		n.cnt = 0;
	}
	for (const l of M.leaves) {
		let n = l.file.node;
		while (n) {
			if (l.x < n.bx0) n.bx0 = l.x;
			if (l.x > n.bx1) n.bx1 = l.x;
			if (l.y < n.by0) n.by0 = l.y;
			if (l.y > n.by1) n.by1 = l.y;
			n.sx += l.x;
			n.sy += l.y;
			n.cnt++;
			n = n.parent;
		}
	}
	for (const n of M.nodes) {
		n.cx = n.sx / Math.max(1, n.cnt);
		n.cy = n.sy / Math.max(1, n.cnt);
		n.rad = Math.max(10, Math.hypot(n.bx1 - n.bx0, n.by1 - n.by0) / 2);
	}
	M.crown.F = [0, 0];
	let wx0 = 1e9, wy0 = 1e9, wx1 = -1e9, wy1 = -1e9;
	for (const l of M.leaves) {
		wx0 = Math.min(wx0, l.x);
		wx1 = Math.max(wx1, l.x);
		wy0 = Math.min(wy0, l.y);
		wy1 = Math.max(wy1, l.y);
	}
	if (!M.leaves.length) {
		wx0 = -40;
		wx1 = 40;
		wy0 = -40;
		wy1 = groundY;
	}
	M.bounds = {
		x0: wx0 - 60,
		y0: wy0 - 60,
		x1: wx1 + 60,
		y1: wy1 + 60
	};
	M.crownBounds = {
		x0: minX,
		x1: maxX,
		y0: minY,
		y1: maxY
	};
	for (const b of M.branches) buildBranchGeom(b);
	for (const b of M.branches) b.subOf = b.node ? b.node.parent : b.owner;
	M.grid = buildGrid(M);
	assignDisplayNames(M);
	M.nameCount = /* @__PURE__ */ new Map();
	for (const f of F) M.nameCount.set(f.name, (M.nameCount.get(f.name) || 0) + 1);
	M.byPath = new Map(F.map((f) => [f.path, f]));
	M.layoutRec = {
		v: 1,
		files: raw.files.length,
		u: unionRecs(ctx),
		memo: [...ctx.memo],
		final: finalRecs(ctx),
		sigs: replay ? replay.sigs : ctx.sigs,
		splits: replay ? replay.splits : [...ctx.splits],
		orders: replay ? replay.orders : [...ctx.orders],
		warmRun: replay ? replay.warmRun : warm ? warm.warmRun + 1 : 0,
		sig: dataSig(raw)
	};
	M.buildStats = {
		memoHits: ctx.memoHits,
		unions: ctx.unions.length,
		replay: !!replay,
		warm: !!warm,
		resolved: ctx.resolved,
		compactMs: Math.round(tCompact),
		passLog: ctx.passLog,
		...ctx.stats
	};
	if (ctx.cache && ctx.ci !== ctx.cache.length) M.replayMismatch = true;
	return M;
}
const BOILER = /* @__PURE__ */ new Set([
	"src",
	"main",
	"java",
	"kotlin",
	"scala",
	"com",
	"org",
	"net",
	"io",
	"pkg",
	"internal"
]);
const normSeg = (s) => s.toLowerCase().replace(/[-_.\s]/g, "");
function assignDisplayNames(M) {
	const crown = M.nodes.filter((n) => n.kind === "crown" && n.depth >= 1);
	const nonFinal = /* @__PURE__ */ new Map();
	for (const n of crown) {
		const s = n.name.split("/");
		for (let i = 0; i < s.length - 1; i++) nonFinal.set(normSeg(s[i]), (nonFinal.get(normSeg(s[i])) || 0) + 1);
	}
	const boiler = (s) => BOILER.has(s.toLowerCase()) || (nonFinal.get(normSeg(s)) || 0) >= 3;
	const said = (n) => {
		const out = /* @__PURE__ */ new Set([normSeg(M.name)]);
		for (let q = n.parent; q && q.depth >= 1; q = q.parent) for (const s of q.name.split("/")) out.add(normSeg(s));
		return out;
	};
	for (const n of crown) {
		const segs = n.name.split("/");
		const anc = said(n);
		if (n.depth === 1 || segs.length === 1 && !(n.depth > 2 && anc.has(normSeg(n.name)))) {
			n.quiet = false;
			n.disp = shortName(n.name);
			n.core = segs.length === 1 ? n.name : baseOf(n.name);
			continue;
		}
		const keep = segs.filter((s) => !boiler(s) && !anc.has(normSeg(s)));
		if (!keep.length) {
			n.quiet = true;
			n.core = null;
			n.disp = (n.parent && n.parent.depth >= 1 ? baseOf(n.parent.name) + "/" : "") + shortName(n.name);
			continue;
		}
		n.quiet = false;
		n.core = keep.join("/");
		n.disp = n.core;
	}
	const telling = (n) => {
		let q = n.parent;
		while (q && q.depth >= 1 && q.quiet) q = q.parent;
		return q && q.depth >= 1 ? q : null;
	};
	for (let round = 0; round < 2; round++) {
		const cnt = /* @__PURE__ */ new Map();
		for (const n of crown) if (!n.quiet) cnt.set(n.disp, (cnt.get(n.disp) || 0) + 1);
		let changed = false;
		for (const n of crown) {
			if (n.quiet || (cnt.get(n.disp) || 0) < 2) continue;
			let q = telling(n);
			for (let k = 0; k < round && q; k++) q = telling(q);
			if (!q) continue;
			const pre = baseOf(q.core || q.name);
			if (!n.disp.startsWith(pre + "/")) {
				n.disp = pre + "/" + n.disp;
				changed = true;
			}
		}
		if (!changed) break;
	}
	for (const n of crown) n.disp = shortName(n.disp);
}
function buildBranchGeom(b) {
	const c = b.cp, SEG = 10;
	const left = [], right = [], pts = [];
	const L = Math.hypot(b.x1 - b.x0, b.y1 - b.y0) || 1;
	for (let i = 0; i <= SEG; i++) {
		const t = i / SEG, mt = 1 - t;
		const x = mt * mt * mt * c[0] + 3 * mt * mt * t * c[2] + 3 * mt * t * t * c[4] + t * t * t * c[6];
		const y = mt * mt * mt * c[1] + 3 * mt * mt * t * c[3] + 3 * mt * t * t * c[5] + t * t * t * c[7];
		let tx = 3 * mt * mt * (c[2] - c[0]) + 6 * mt * t * (c[4] - c[2]) + 3 * t * t * (c[6] - c[4]);
		let ty = 3 * mt * mt * (c[3] - c[1]) + 6 * mt * t * (c[5] - c[3]) + 3 * t * t * (c[7] - c[5]);
		const tl = Math.hypot(tx, ty) || 1;
		tx /= tl;
		ty /= tl;
		const w = taperHW(b.wb, b.we, t);
		pts.push([
			x,
			y,
			tx,
			ty
		]);
		left.push(x - ty * w, y + tx * w);
		right.push(x + ty * w, y - tx * w);
	}
	b.c = c.slice();
	b.len = L;
	b.pts = pts;
	const poly = /* @__PURE__ */ new Float32Array(44);
	for (let i = 0; i <= SEG; i++) {
		poly[i * 2] = left[i * 2];
		poly[i * 2 + 1] = left[i * 2 + 1];
	}
	for (let i = 0; i <= SEG; i++) {
		const j = SEG - i;
		poly[22 + i * 2] = right[j * 2];
		poly[22 + i * 2 + 1] = right[j * 2 + 1];
	}
	b.poly = poly;
	let bx0 = 1e9, by0 = 1e9, bx1 = -1e9, by1 = -1e9;
	for (const q of pts) {
		bx0 = Math.min(bx0, q[0]);
		bx1 = Math.max(bx1, q[0]);
		by0 = Math.min(by0, q[1]);
		by1 = Math.max(by1, q[1]);
	}
	b.bx0 = bx0 - b.wb;
	b.bx1 = bx1 + b.wb;
	b.by0 = by0 - b.wb;
	b.by1 = by1 + b.wb;
}
function buildGrid(M) {
	const G = 40, cells = /* @__PURE__ */ new Map();
	const key = (i, j) => i * 100003 + j;
	const add = (x, y, item) => {
		const k = key(Math.floor(x / G), Math.floor(y / G));
		let a = cells.get(k);
		if (!a) cells.set(k, a = []);
		a.push(item);
	};
	for (const l of M.leaves) add(l.x, l.y, { leaf: l });
	for (const b of M.branches) for (const p of b.pts) add(p[0], p[1], {
		br: b,
		p
	});
	return {
		G,
		near(x, y, r) {
			const out = [];
			const i0 = Math.floor((x - r) / G), i1 = Math.floor((x + r) / G), j0 = Math.floor((y - r) / G), j1 = Math.floor((y + r) / G);
			for (let i = i0; i <= i1; i++) for (let j = j0; j <= j1; j++) {
				const a = cells.get(key(i, j));
				if (a) for (const it of a) out.push(it);
			}
			return out;
		}
	};
}
//#endregion
//#region src/lib/codetree/layout.worker.ts
const post = (m) => self.postMessage(m);
self.onmessage = (e) => {
	const { id, raw, warm } = e.data;
	let last = 0;
	try {
		const t0 = performance.now();
		const M = buildModel(raw, {
			quantise: true,
			warm,
			tick: (k) => {
				const now = performance.now();
				if (now - last > 90) {
					last = now;
					post({
						id,
						progress: k
					});
				}
			}
		});
		post({
			id,
			rec: M.layoutRec,
			ms: Math.round(performance.now() - t0),
			stats: M.buildStats
		});
	} catch (err) {
		post({
			id,
			error: String(err?.message || err)
		});
	}
};
//#endregion
