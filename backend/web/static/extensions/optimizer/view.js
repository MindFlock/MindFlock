/** The Usage Optimizer dashboard: one renderer for the dialog and the pane.
 *
 * Fetches GET /api/optimizer/summary?range=, draws it, and refreshes every
 * minute while mounted (plus a quick refetch when a provider window reopens
 * or a session flips to/from the limit screen). Range (24h / 7d / 30d) and
 * the selected tab are remembered per browser in api.storage. */

import { fmtDur, fmtHours, fmtClock, fmtDay, fmtTokens, gauge, paceTone, scoreRing, sparkline, stateLabel, uptimeStrip, windowBars } from "./charts.js";

const REFRESH_MS = 60000;
const RANGES = ["24h", "7d", "30d"];

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (/^on[A-Z]/.test(k)) n.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k in n) n[k] = v;
      else n.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return n;
}

function paceWords(ratio) {
  if (ratio === null || ratio === undefined) return "too early to tell";
  if (ratio > 1.4) return "draining " + ratio.toFixed(1) + "× faster than the window";
  if (ratio > 1.15) return "a little ahead of the window (" + ratio.toFixed(1) + "×)";
  if (ratio < 0.6) return "well under pace (" + ratio.toFixed(1) + "×) — capacity going unused";
  return "on pace (" + ratio.toFixed(1) + "×)";
}

function liveCard(p, now) {
  const live = p.live;
  if (p.mode !== "windowed") {
    return el("div", { class: "uo-card" }, el("h4", { text: "This window" }), el("p", { class: "muted", text: "Pay-per-token: there is no window to fill, only spend. The uptime strip still shows what the sessions did." }));
  }
  if (!live) {
    return el(
      "div",
      { class: "uo-card" },
      el("h4", { text: "This window" }),
      el("p", { class: "muted", text: "No active window: nothing has been sent since the last reset. The next message starts one." })
    );
  }
  const tone = paceTone(live.ratio);
  const rows = [
    el("div", { class: "uo-big" }, el("span", { class: "uo-num " + tone, text: Math.round(live.pct) + "%" }), el("span", { class: "muted", text: " used · resets in " + fmtDur(live.remaining_s) + " (" + fmtClock(live.end) + ")" })),
    gauge(live.pct, live.elapsed_frac, live.projected, tone),
    el("p", { class: "uo-pace " + tone, text: paceWords(live.ratio) }),
  ];
  const facts = [];
  if (live.rate_pct_h !== null && live.rate_pct_h !== undefined) facts.push("burning " + live.rate_pct_h.toFixed(live.rate_pct_h < 10 ? 1 : 0) + "%/h");
  if (live.eta_limit) facts.push("limit in " + fmtDur(live.eta_limit - now));
  else facts.push("projected " + Math.round(live.projected) + "% at reset");
  if (live.src && live.src !== "live") facts.push("estimated (" + live.src + ")");
  rows.push(el("p", { class: "muted uo-facts", text: facts.join(" · ") }));
  return el("div", { class: "uo-card" }, el("h4", { text: "This window" }), ...rows);
}

function weeklyCard(p, now) {
  const w = p.weekly;
  if (p.mode !== "windowed" || !p.weekly_hours) return null;
  if (!w) return el("div", { class: "uo-card" }, el("h4", { text: "Weekly cap" }), el("p", { class: "muted", text: "No weekly reading from this provider yet." }));
  const tone = paceTone(w.ratio);
  const facts = [w.pct >= 100 ? "cap reached — locked out until the reset" : w.ratio !== null && w.ratio !== undefined ? paceWords(w.ratio) : "too early to tell"];
  if (w.pct >= 100) {
    /* nothing to project */
  } else if (w.eta_limit) facts.push("runs out in " + fmtDur(w.eta_limit - now) + ", " + fmtDur(w.end - w.eta_limit) + " before the reset");
  else facts.push("projected " + Math.round(w.projected) + "% at reset");
  return el(
    "div",
    { class: "uo-card" },
    el("h4", { text: "Weekly cap" }),
    el("div", { class: "uo-big" }, el("span", { class: "uo-num " + tone, text: Math.round(w.pct) + "%" }), el("span", { class: "muted", text: " used · resets " + fmtDay(w.end) + " " + fmtClock(w.end) + " (in " + fmtDur(w.remaining_s) + ")" })),
    gauge(w.pct, w.elapsed_frac, w.projected, tone),
    el("p", { class: "uo-pace " + tone, text: facts.join(" · ") })
  );
}

function scoreCard(p, rangeLabel) {
  const s = p.score || {};
  const note =
    s.score === null || s.score === undefined
      ? "Appears after the first window reset the optimizer has watched."
      : s.windows + " window" + (s.windows === 1 ? "" : "s") + " reset in the last " + rangeLabel + (s.avg_fill !== null && s.avg_fill !== undefined ? ", filled " + Math.round(s.avg_fill) + "% on average" : "") + ". Ceiling: " + (Math.round(s.ceiling * 10) / 10) + " windows could have been filled.";
  return el(
    "div",
    { class: "uo-card uo-score-card" },
    el("h4", { text: "Capacity used" }),
    el("div", { class: "uo-score-row" }, scoreRing(s.score, 72), el("div", {}, el("p", { class: "uo-score-what", text: "Share of the plan capacity your windows actually used. 100 = every window reset full." }), el("p", { class: "muted", text: note }), basisNote(p)))
  );
}

/** Where a percent came from when the provider's own meter did not answer
 * (Claude's usage endpoint rate-limits; Codex has no fresh snapshot). */
function basisNote(p) {
  const cal = p.calibration || {};
  const basis = (p.score && p.score.basis) || (p.live && p.live.src) || "";
  if (!basis || basis === "live") return null;
  const cap = cal.capacity ? "$" + Math.round(cal.capacity) : "";
  const text = {
    budget: "No live meter: percent is the window's estimated API cost against your window budget (" + cap + ").",
    learned: "No live meter: percent is the window's estimated API cost against what a window cost when you last hit the limit (" + cap + ").",
    provisional: "No live meter and no lockout seen yet: percent is relative to your most expensive window so far (" + cap + "). It firms up after the first lockout, or set a window budget in Settings.",
    estimate: "Some readings are transcript estimates, not the provider's own meter.",
  }[basis] || "Readings are estimates, not the provider's own meter.";
  return el("p", { class: "muted uo-basis", text });
}

function adviceList(items) {
  if (!items || !items.length) return null;
  return el(
    "div",
    { class: "uo-advice" },
    ...items.map((a) => el("div", { class: "uo-tip " + (a.level || "tip") }, el("div", { class: "uo-tip-title", text: a.title }), el("div", { class: "uo-tip-text", text: a.text })))
  );
}

function splitTable(split) {
  const rows = ["working", "idle", "limited", "offline"];
  const label = { working: "Agents working", idle: "Agents idle", limited: "Locked out", offline: "No sessions" };
  const at = split.attended || {};
  const un = split.unattended || {};
  const total = Math.max(1, (split.total || 0) - (split.unknown || 0));
  const pct = (s) => Math.round((100 * (s || 0)) / total) + "%";
  const tbl = el(
    "table",
    { class: "uo-split" },
    el("thead", {}, el("tr", {}, el("th", { text: "" }), el("th", { text: "You at the keyboard" }), el("th", { text: "Running on its own" }))),
    el(
      "tbody",
      {},
      ...rows.map((r) => el("tr", { class: r }, el("td", { text: label[r] }), el("td", { text: fmtHours(at[r]) + " · " + pct(at[r]) }), el("td", { text: fmtHours(un[r]) + " · " + pct(un[r]) })))
    )
  );
  const unknown = split.unknown || 0;
  return el("div", {}, tbl, unknown > 60 ? el("p", { class: "muted", text: "Not watched (server off): " + fmtHours(unknown) + "." }) : null);
}

function legend() {
  const items = [
    ["working", "agents working"],
    ["idle", "agents idle"],
    ["limited", "locked out"],
    ["offline", "no sessions"],
    ["unknown", "not watching"],
  ];
  return el(
    "div",
    { class: "uo-legend" },
    el("span", { class: "uo-leg" }, el("i", { class: "uo-sw att" }), "you were here"),
    ...items.map(([k, t]) => el("span", { class: "uo-leg" }, el("i", { class: "uo-sw " + k }), t))
  );
}

function providerSection(p, data, rangeLabel) {
  const now = data.now;
  const since = now - data.period_s;
  const parts = [];
  parts.push(el("div", { class: "uo-cards" }, scoreCard(p, rangeLabel), liveCard(p, now), weeklyCard(p, now)));
  parts.push(adviceList(p.advice));
  if (p.mode === "windowed") {
    parts.push(
      el(
        "section",
        { class: "uo-chart" },
        el("h4", { text: "Window fill" }),
        el("p", { class: "muted", text: "One bar per plan window, at its reset time; height is how much of it was used. Blue bars ran mostly while you were here, grey ones on their own. The outlined bar is the live window." }),
        p.windows && p.windows.length ? windowBars(p.windows, since, now, p.window_hours) : el("p", { class: "muted", text: "No windows recorded in this range yet." })
      )
    );
  }
  parts.push(
    el(
      "section",
      { class: "uo-chart" },
      el("h4", { text: "Uptime" }),
      el("p", { class: "muted", text: "What this provider's sessions were doing, and (the thin row on top) when you were typing into MindFlock." }),
      uptimeStrip(p.timeline || [], since, now),
      legend()
    )
  );
  parts.push(el("section", { class: "uo-chart" }, el("h4", { text: "Time split" }), splitTable(p.split || {})));
  if (p.tokens && p.tokens.length) {
    parts.push(el("section", { class: "uo-chart" }, el("h4", { text: "Tokens per " + (data.bucket_s >= 3600 ? Math.round(data.bucket_s / 3600) + "h" : Math.round(data.bucket_s / 60) + "m") }), sparkline(p.tokens, since, now)));
  }
  return el("div", { class: "uo-provider" }, ...parts);
}

function overallSection(data, rangeLabel) {
  const provs = data.providers || [];
  const cards = provs.map((p) => {
    const s = p.score || {};
    const live = p.live;
    const tone = live ? paceTone(live.ratio) : "";
    return el(
      "div",
      { class: "uo-card uo-mini" },
      el("div", { class: "uo-mini-head" }, scoreRing(s.score, 48), el("div", {}, el("div", { class: "uo-mini-name", text: p.label }), el("div", { class: "muted", text: p.mode === "windowed" ? (live ? Math.round(live.pct) + "% of this window · " + paceWords(live.ratio) : "no active window") : "pay-per-token" }))),
      live ? gauge(live.pct, live.elapsed_frac, live.projected, tone) : null,
      p.weekly ? el("div", { class: "muted uo-facts", text: "weekly cap " + Math.round(p.weekly.pct) + "% · resets in " + fmtDur(p.weekly.remaining_s) }) : null
    );
  });
  const tips = [];
  for (const p of provs) for (const a of p.advice || []) if (a.level !== "ok") tips.push(a);
  const ok = provs.every((p) => (p.advice || []).every((a) => a.level === "ok"));
  return el(
    "div",
    { class: "uo-provider" },
    el("div", { class: "uo-cards" }, ...cards),
    tips.length ? adviceList(tips) : ok && provs.length ? el("div", { class: "uo-advice" }, el("div", { class: "uo-tip ok" }, el("div", { class: "uo-tip-title", text: "Nothing to fix right now" }), el("div", { class: "uo-tip-text", text: "Windows are filling at a sustainable pace. Pick a provider tab for its charts." }))) : null,
    el("p", { class: "muted uo-how" }, "How the score works: over the last " + rangeLabel + " a provider with a " + (provs[0] && provs[0].window_hours ? provs[0].window_hours + "-hour" : "rolling") + " window could have filled a fixed number of windows. The score is the share of that ceiling the windows you actually ran reached — a window that reset at 40% counts 0.4, one you never opened counts 0. Pace compares the share used with the share of the window elapsed: above 1× you will hit the limit before the reset; below it capacity goes unused.")
  );
}

export function renderBoard(api, host, opts) {
  const root = host.el;
  root.classList.add("uo-root");
  if (opts && opts.compact) root.classList.add("uo-compact");
  let range = api.storage.get("range", "7d");
  if (!RANGES.includes(range)) range = "7d";
  let tab = api.storage.get("tab", "overall");
  let data = null;
  let timer = null;
  let disposed = false;
  let unsubs = [];

  const status = el("span", { class: "uo-status muted" });
  const attended = el("span", { class: "uo-att-chip" });
  const rangeBtns = RANGES.map((r) => el("button", { class: "uo-btn" + (r === range ? " on" : ""), type: "button", text: r, onClick: () => setRange(r) }));
  const refreshBtn = el("button", { class: "uo-btn", type: "button", text: "Refresh", title: "Fetch the latest readings", onClick: () => load(true) });
  const sampleBtn = el("button", { class: "uo-btn", type: "button", text: "Sample now", title: "Record a usage sample this second", onClick: async () => {
    try {
      await api.commands.run("optimizer.sample");
      load(true);
    } catch (err) {
      api.log.error("sample", err);
    }
  } });
  const windowBtn = opts && !opts.compact ? el("button", { class: "uo-btn", type: "button", text: "Open as window", onClick: () => api.ui.openPane("board", { title: "Usage Optimizer" }) }) : null;
  const headRing = el("div", { class: "uo-head-ring" });
  const headText = el("div", { class: "uo-head-text" });
  const top = el("div", { class: "uo-top" }, el("div", { class: "uo-head" }, headRing, headText), el("div", { class: "uo-controls" }, attended, ...rangeBtns, refreshBtn, sampleBtn, windowBtn, status));
  const tabs = el("div", { class: "uo-tabs" });
  const body = el("div", { class: "uo-body" }, el("p", { class: "muted", text: "Loading usage history…" }));
  root.appendChild(el("div", { class: "uo-wrap" }, top, tabs, body));

  function setRange(r) {
    range = r;
    api.storage.set("range", r);
    rangeBtns.forEach((b) => b.classList.toggle("on", b.textContent === r));
    load(true);
  }

  function setTab(t) {
    tab = t;
    api.storage.set("tab", t);
    draw();
  }

  function rangeLabel() {
    return { "24h": "24 hours", "7d": "7 days", "30d": "30 days" }[range] || range;
  }

  function draw() {
    if (disposed || !data) return;
    const provs = data.providers || [];
    if (tab !== "overall" && !provs.some((p) => p.name === tab)) tab = "overall";
    tabs.replaceChildren(
      el("button", { class: "uo-tab" + (tab === "overall" ? " on" : ""), type: "button", text: "Overall", onClick: () => setTab("overall") }),
      ...provs.map((p) => el("button", { class: "uo-tab" + (tab === p.name ? " on" : ""), type: "button", text: p.label, onClick: () => setTab(p.name) }))
    );
    headRing.replaceChildren(scoreRing(data.overall, 64));
    headText.replaceChildren(
      el("div", { class: "uo-head-title", text: data.overall === null || data.overall === undefined ? "No score yet" : "Capacity used, last " + rangeLabel() }),
      el("div", { class: "muted", text: data.overall === null || data.overall === undefined ? "The score appears after the first plan window the optimizer watched resets." : "Mean across " + provs.filter((p) => p.score && p.score.score !== null).length + " provider" + (provs.filter((p) => p.score && p.score.score !== null).length === 1 ? "" : "s") + ". Every window that resets below 100% is capacity gone for good." })
    );
    attended.textContent = data.attended_now ? "You're here" : "Unattended";
    attended.className = "uo-att-chip " + (data.attended_now ? "here" : "away");
    attended.title = data.attended_now ? "Someone typed into a MindFlock window in the last " + Math.round(data.attended_s / 60) + " minutes" : "No keystrokes into MindFlock for " + Math.round(data.attended_s / 60) + "+ minutes: this time counts as running on its own";
    if (!provs.length) {
      body.replaceChildren(el("p", { class: "muted", text: "No providers to watch: the optimizer follows the CLIs the cost panel shows (Claude, Codex, Antigravity, Aider)." }));
      return;
    }
    const p = provs.find((x) => x.name === tab);
    body.replaceChildren(p ? providerSection(p, data, rangeLabel()) : overallSection(data, rangeLabel()));
  }

  async function load(manual) {
    if (disposed) return;
    status.textContent = manual ? "refreshing…" : "";
    try {
      const r = await api.request("/api/optimizer/summary?range=" + encodeURIComponent(range));
      if (disposed) return;
      if (r && r.error) throw new Error(r.error);
      data = r;
      draw();
      status.textContent = "updated " + fmtClock(data.now);
    } catch (err) {
      api.log.error("summary failed", err);
      status.textContent = "could not load";
      if (!data) body.replaceChildren(el("p", { class: "muted", text: "Could not reach the server." }));
    }
  }

  function schedule() {
    timer = setInterval(() => load(false), REFRESH_MS);
  }

  load(false);
  schedule();
  if (api.events && typeof api.events.subscribe === "function") {
    let pending = null;
    const soon = () => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = null;
        load(false);
      }, 1500);
    };
    try {
      unsubs.push(api.events.subscribe("session.usage_restored", soon));
      unsubs.push(
        api.events.subscribe("session.activity_changed", (env) => {
          if (env && (env.new === "limit" || env.old === "limit")) soon();
        })
      );
    } catch (err) {
      api.log.error("events", err);
    }
  }
  return {
    dispose() {
      disposed = true;
      if (timer) clearInterval(timer);
      for (const u of unsubs) {
        try {
          if (typeof u === "function") u();
        } catch (_) {
          /* ignore */
        }
      }
      root.replaceChildren();
    },
  };
}
