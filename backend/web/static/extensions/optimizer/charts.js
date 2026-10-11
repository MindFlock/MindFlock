/** Inline-SVG charts for the Usage Optimizer. Plain DOM, theme tokens only
 * (currentColor + CSS variables), no library: a gauge with a pace marker,
 * the window-fill bars, the uptime strip, a token sparkline and the score
 * ring. Every function returns a detached element the view appends. */

const NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs) {
  const n = document.createElementNS(NS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) n.setAttribute(k, String(v));
  return n;
}

function text(x, y, s, cls, anchor) {
  const t = svg("text", { x, y, class: cls || "", "text-anchor": anchor || "start" });
  t.textContent = s;
  return t;
}

export function pad2(n) {
  return (n < 10 ? "0" : "") + n;
}

export function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  const m = Math.round(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  const rm = m % 60;
  if (h < 24) return rm ? h + "h " + rm + "m" : h + "h";
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? d + "d " + rh + "h" : d + "d";
}

export function fmtHours(s) {
  const h = Math.max(0, s) / 3600;
  if (h < 0.1) return "0h";
  return (h < 10 ? h.toFixed(1) : Math.round(h)) + "h";
}

export function fmtClock(epoch) {
  const d = new Date(epoch * 1000);
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

export function fmtDay(epoch) {
  const d = new Date(epoch * 1000);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export function fmtTokens(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "k";
  return String(n);
}

/** Pace → a tone class. ratio: used-share / elapsed-share. */
export function paceTone(ratio) {
  if (ratio === null || ratio === undefined) return "";
  if (ratio > 1.4) return "hot";
  if (ratio > 1.15) return "warm";
  if (ratio < 0.6) return "cold";
  return "ok";
}

/** A horizontal gauge: the used share, a tick where "even pace" would be
 * (the elapsed share of the window), and the projection as a hatched tail. */
export function gauge(pct, elapsedFrac, projected, tone) {
  const W = 300;
  const H = 18;
  const root = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "uo-gauge " + (tone || ""), preserveAspectRatio: "none" });
  root.appendChild(svg("rect", { x: 0, y: 4, width: W, height: 10, rx: 5, class: "uo-g-track" }));
  const used = Math.max(0, Math.min(100, pct || 0));
  if (projected !== null && projected !== undefined && projected > used) {
    const p = Math.max(used, Math.min(100, projected));
    root.appendChild(svg("rect", { x: 0, y: 4, width: (W * p) / 100, height: 10, rx: 5, class: "uo-g-proj" }));
  }
  if (used > 0) root.appendChild(svg("rect", { x: 0, y: 4, width: Math.max(6, (W * used) / 100), height: 10, rx: 5, class: "uo-g-used" }));
  if (elapsedFrac !== null && elapsedFrac !== undefined) {
    const x = W * Math.max(0, Math.min(1, elapsedFrac));
    root.appendChild(svg("line", { x1: x, x2: x, y1: 0, y2: H, class: "uo-g-pace" }));
  }
  return root;
}

/** The score ring: 0–100 as an arc, the number in the middle. */
export function scoreRing(score, size) {
  size = size || 96;
  const r = size / 2 - 7;
  const c = 2 * Math.PI * r;
  const root = svg("svg", { viewBox: "0 0 " + size + " " + size, width: size, height: size, class: "uo-ring" });
  root.appendChild(svg("circle", { cx: size / 2, cy: size / 2, r, class: "uo-ring-track" }));
  if (score !== null && score !== undefined) {
    const tone = score >= 75 ? "good" : score >= 45 ? "mid" : "low";
    root.appendChild(
      svg("circle", {
        cx: size / 2,
        cy: size / 2,
        r,
        class: "uo-ring-arc " + tone,
        "stroke-dasharray": c.toFixed(2),
        "stroke-dashoffset": (c * (1 - Math.max(0, Math.min(100, score)) / 100)).toFixed(2),
        transform: "rotate(-90 " + size / 2 + " " + size / 2 + ")",
      })
    );
  }
  const label = text(size / 2, size / 2 + (size > 60 ? 7 : 5), score === null || score === undefined ? "–" : Math.round(score), "uo-ring-num", "middle");
  root.appendChild(label);
  return root;
}

/** Window-fill bars over the range: one bar per window at its reset time,
 * height = final %; the live window is outlined. Attended share tints the
 * bar: fully attended = accent, unattended = the "auto" tone. */
export function windowBars(windows, since, now, windowHours) {
  const W = 640;
  const H = 150;
  const padL = 30;
  const padB = 22;
  const padT = 8;
  const root = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "uo-bars", preserveAspectRatio: "none" });
  const plotW = W - padL - 6;
  const plotH = H - padB - padT;
  const x = (t) => padL + ((t - since) / (now - since)) * plotW;
  [0, 50, 100].forEach((p) => {
    const y = padT + plotH * (1 - p / 100);
    root.appendChild(svg("line", { x1: padL, x2: W - 6, y1: y, y2: y, class: "uo-grid" }));
    root.appendChild(text(padL - 4, y + 3, p + "%", "uo-axis", "end"));
  });
  const span = (windowHours > 0 ? windowHours * 3600 : 3600) / (now - since);
  const barW = Math.max(3, Math.min(28, plotW * span - 2));
  for (const w of windows) {
    const x1 = Math.max(padL, x(w.start));
    const x2 = Math.min(W - 6, x(Math.min(w.end, now)));
    const bw = Math.max(3, Math.min(barW, x2 - x1));
    const h = plotH * (w.final_pct / 100);
    const cls = "uo-bar " + (w.live ? "live " : "") + (w.attended_share >= 0.5 ? "attended" : "auto");
    const rect = svg("rect", { x: x1, y: padT + plotH - h, width: bw, height: Math.max(1, h), rx: 2, class: cls });
    const title = svg("title");
    title.textContent =
      (w.live ? "Live window · " : "Window reset " + fmtDay(w.end) + " " + fmtClock(w.end) + " · ") +
      w.final_pct +
      "% used · " +
      Math.round(w.attended_share * 100) +
      "% of it with you at the keyboard";
    rect.appendChild(title);
    root.appendChild(rect);
  }
  // time axis: a few labels
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = since + ((now - since) * i) / ticks;
    const label = now - since > 2 * 86400 ? fmtDay(t) : fmtClock(t);
    root.appendChild(text(x(t), H - 6, label, "uo-axis", i === 0 ? "start" : i === ticks ? "end" : "middle"));
  }
  return root;
}

/** The uptime strip: one cell per bucket coloured by state, with a thin
 * "attended" row on top. */
export function uptimeStrip(buckets, since, now) {
  const W = 640;
  const H = 44;
  const root = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "uo-strip", preserveAspectRatio: "none" });
  const n = buckets.length || 1;
  const cw = W / n;
  buckets.forEach((b, i) => {
    const x = i * cw;
    const cell = svg("rect", { x, y: 10, width: cw + 0.5, height: 22, class: "uo-cell " + b.state });
    const t = svg("title");
    t.textContent =
      fmtDay(b.t) + " " + fmtClock(b.t) + " · " + stateLabel(b.state) + (b.att ? " · you were here" : "") + (b.pct !== null && b.pct !== undefined ? " · window " + b.pct + "%" : "");
    cell.appendChild(t);
    root.appendChild(cell);
    if (b.att) root.appendChild(svg("rect", { x, y: 3, width: cw + 0.5, height: 4, class: "uo-att" }));
  });
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = since + ((now - since) * i) / ticks;
    const label = now - since > 2 * 86400 ? fmtDay(t) : fmtClock(t);
    root.appendChild(text((W * i) / ticks, H - 2, label, "uo-axis", i === 0 ? "start" : i === ticks ? "end" : "middle"));
  }
  return root;
}

export function stateLabel(s) {
  return { working: "agents working", idle: "agents idle", limited: "locked out (usage limit)", offline: "no sessions", unknown: "not watching" }[s] || s;
}

/** Tokens per bucket as columns. */
export function sparkline(points, since, now) {
  const W = 640;
  const H = 70;
  const root = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "uo-spark", preserveAspectRatio: "none" });
  const n = points.length || 1;
  const cw = W / n;
  const max = Math.max(1, ...points.map((p) => p.tokens));
  points.forEach((p, i) => {
    if (!p.tokens) return;
    const h = ((H - 16) * p.tokens) / max;
    const r = svg("rect", { x: i * cw + 0.5, y: H - 14 - h, width: Math.max(1, cw - 1), height: h, class: "uo-spark-bar" });
    const t = svg("title");
    t.textContent = fmtDay(p.t) + " " + fmtClock(p.t) + " · " + fmtTokens(p.tokens) + " tokens";
    r.appendChild(t);
    root.appendChild(r);
  });
  root.appendChild(text(2, 10, "peak " + fmtTokens(max), "uo-axis"));
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = since + ((now - since) * i) / ticks;
    const label = now - since > 2 * 86400 ? fmtDay(t) : fmtClock(t);
    root.appendChild(text((W * i) / ticks, H - 2, label, "uo-axis", i === 0 ? "start" : i === ticks ? "end" : "middle"));
  }
  return root;
}
