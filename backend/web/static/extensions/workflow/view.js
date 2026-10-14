/** The Workflow Optimizer dashboard: one renderer for the dialog and the pane.
 *
 * GET /api/workflow/summary?range= → the headline (median time to PR), where
 * that time goes (phases, grouped by who is the bottleneck), what to change,
 * the weekly trend and every work item; a work item expands into its own
 * timeline (GET /api/workflow/item?id=). Refreshes every minute while mounted
 * and soon after a stage change or a merge. Range and the phase-table view
 * are remembered per browser in api.storage. */

const REFRESH_MS = 60000;
const RANGES = ["7d", "30d", "90d"];
const STATUS_WORDS = {
  merged: "merged",
  pr_open: "PR open",
  in_progress: "in progress",
  abandoned: "abandoned",
  closed: "closed",
};

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

export function fmtDur(s) {
  if (s === null || s === undefined) return "—";
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

function fmtWhen(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function pct(share) {
  const v = Math.round((share || 0) * 100);
  return v < 1 && share > 0 ? "<1%" : v + "%";
}

/** A stacked bar of phase seconds: {phase: seconds}. Each piece is titled. */
export function phaseBar(totals, labels, cls) {
  const entries = Object.entries(totals || {}).filter(([, v]) => v > 0);
  const sum = entries.reduce((a, [, v]) => a + v, 0);
  const bar = el("div", { class: "wf-bar" + (cls ? " " + cls : "") });
  if (!sum) return bar;
  const order = Object.keys(labels);
  entries.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  for (const [ph, v] of entries) {
    const piece = el("span", { class: "wf-seg ph-" + ph, title: (labels[ph] || ph) + " — " + fmtDur(v) + " (" + pct(v / sum) + ")" });
    piece.style.flexGrow = String(v);
    bar.appendChild(piece);
  }
  return bar;
}

/** An item's real timeline: segments placed on a time axis. */
export function timelineStrip(timeline, labels) {
  const strip = el("div", { class: "wf-strip" });
  if (!timeline || !timeline.length) return strip;
  const a = timeline[0].start;
  const span = Math.max(1, timeline[timeline.length - 1].end - a);
  for (const sg of timeline) {
    const piece = el("span", { class: "wf-seg ph-" + sg.ph, title: (labels[sg.ph] || sg.ph) + " — " + fmtDur(sg.end - sg.start) + " from " + fmtWhen(sg.start) });
    piece.style.left = ((100 * (sg.start - a)) / span).toFixed(3) + "%";
    piece.style.width = Math.max(0.15, (100 * (sg.end - sg.start)) / span).toFixed(3) + "%";
    strip.appendChild(piece);
  }
  return strip;
}

function legend(phases, present) {
  return el(
    "div",
    { class: "wf-legend" },
    phases.filter((p) => present.has(p.id)).map((p) => el("span", { class: "wf-key", title: p.hint }, el("i", { class: "wf-swatch ph-" + p.id }), p.label))
  );
}

function kpis(data, rangeLabel) {
  const k = data.kpi || {};
  const tile = (num, label, sub) => el("div", { class: "wf-kpi" }, el("div", { class: "wf-kpi-num", text: num }), el("div", { class: "wf-kpi-label", text: label }), sub ? el("div", { class: "muted wf-kpi-sub", text: sub }) : null);
  return el(
    "div",
    { class: "wf-kpis" },
    tile(fmtDur(k.median_to_pr), "median time to PR", k.p90_to_pr !== null && k.p90_to_pr !== undefined ? "p90 " + fmtDur(k.p90_to_pr) : "no PR opened in the last " + rangeLabel),
    tile(fmtDur(k.median_active_to_pr), "median active time to PR", "without idle-while-away and untracked time"),
    tile(fmtDur(k.median_to_merge), "median time to merge", (k.merged || 0) + " merged"),
    tile(String(k.prs_opened || 0), "PRs opened", (k.items || 0) + " work items"),
    tile(String(k.in_progress || 0), "in progress", "not at a PR yet")
  );
}

function groupLine(groups) {
  return el(
    "div",
    { class: "wf-groups" },
    (groups || []).map((g) => el("span", { class: "wf-group g-" + g.group }, el("b", { text: pct(g.share) }), " " + g.label))
  );
}

function adviceList(items) {
  if (!items || !items.length) return null;
  return el(
    "div",
    { class: "wf-advice" },
    items.map((a) =>
      el(
        "div",
        { class: "wf-tip " + (a.level || "warn") },
        el("div", { class: "wf-tip-title" }, el("i", { class: "wf-swatch ph-" + a.phase }), a.title, el("span", { class: "muted wf-tip-stake", text: " · " + fmtDur(a.at_stake_s) + " (" + pct(a.share) + ")" })),
        el("div", { class: "wf-tip-text", text: a.detail })
      )
    )
  );
}

function phaseTable(rows) {
  if (!rows || !rows.length) return el("p", { class: "muted", text: "Nothing recorded for this span yet." });
  const max = Math.max(...rows.map((r) => r.share || 0), 0.0001);
  return el(
    "table",
    { class: "wf-table" },
    el("thead", {}, el("tr", {}, el("th", { text: "Phase" }), el("th", { text: "Share" }), el("th", { text: "Total" }), el("th", { text: "Median / item" }), el("th", { text: "p90 / item" }), el("th", { text: "Items" }))),
    el(
      "tbody",
      {},
      rows.map((r) => {
        const fill = el("span", { class: "wf-share-fill ph-" + r.phase });
        fill.style.width = ((100 * (r.share || 0)) / max).toFixed(1) + "%";
        return el(
          "tr",
          { title: r.hint },
          el("td", {}, el("i", { class: "wf-swatch ph-" + r.phase }), r.label),
          el("td", { class: "wf-share" }, el("span", { class: "wf-share-track" }, fill), el("span", { class: "wf-share-num", text: pct(r.share) })),
          el("td", { text: fmtDur(r.total_s) }),
          el("td", { text: fmtDur(r.median_s) }),
          el("td", { text: fmtDur(r.p90_s) }),
          el("td", { text: String(r.items) })
        );
      })
    )
  );
}

function trendChart(trend) {
  const vals = (trend || []).map((w) => w.median_to_pr);
  if (trend.length < 2 || !vals.some((v) => v !== null && v !== undefined)) return null;
  const max = Math.max(...vals.filter((v) => v !== null && v !== undefined), 1);
  return el(
    "div",
    { class: "wf-card" },
    el("h4", { text: "Median time to PR, by week" }),
    el(
      "div",
      { class: "wf-trend" },
      trend.map((w) => {
        const h = w.median_to_pr ? Math.max(4, (100 * w.median_to_pr) / max) : 0;
        const bar = el("span", { class: "wf-trend-bar" + (w.median_to_pr ? "" : " empty") });
        bar.style.height = h.toFixed(1) + "%";
        return el(
          "div",
          { class: "wf-trend-col", title: "Week ending " + fmtWhen(w.end) + ": " + (w.median_to_pr ? fmtDur(w.median_to_pr) + " median over " + w.items + " PR" + (w.items === 1 ? "" : "s") : "no PRs") },
          el("div", { class: "wf-trend-plot" }, bar),
          el("div", { class: "muted wf-trend-label", text: w.median_to_pr ? fmtDur(w.median_to_pr) : "–" })
        );
      })
    )
  );
}

function itemTitle(it) {
  const bits = [];
  if (it.ticket) bits.push(it.ticket);
  const name = it.name && it.name !== it.ticket ? it.name : "";
  if (name) bits.push(name);
  return bits.join(" · ") || it.branch || it.key;
}

export function renderBoard(api, host, opts) {
  const root = host.el;
  root.classList.add("wf-root");
  if (opts && opts.compact) root.classList.add("wf-compact");
  let range = api.storage.get("range", "7d");
  if (!RANGES.includes(range)) range = "7d";
  let view = api.storage.get("view", "to_pr");
  let data = null;
  let timer = null;
  let disposed = false;
  const open = new Set();
  const details = new Map();
  const unsubs = [];

  const status = el("span", { class: "wf-status muted" });
  const rangeBtns = RANGES.map((r) => el("button", { class: "wf-btn" + (r === range ? " on" : ""), type: "button", text: r, onClick: () => setRange(r) }));
  const refreshBtn = el("button", { class: "wf-btn", type: "button", text: "Refresh", onClick: () => load(true) });
  const windowBtn = opts && !opts.compact ? el("button", { class: "wf-btn", type: "button", text: "Open as window", onClick: () => api.ui.openPane("board", { title: "Workflow Optimizer" }) }) : null;
  const top = el("div", { class: "wf-top" }, el("div", { class: "wf-head" }, el("div", { class: "wf-head-title", text: "Where your time goes, ticket → PR" }), el("div", { class: "muted", text: "Every work item (one branch's trip to a merged PR), split by what was holding it." })), el("div", { class: "wf-controls" }, ...rangeBtns, refreshBtn, windowBtn, status));
  const body = el("div", { class: "wf-body" }, el("p", { class: "muted", text: "Loading the workflow timeline…" }));
  root.appendChild(el("div", { class: "wf-wrap" }, top, body));

  function setRange(r) {
    range = r;
    api.storage.set("range", r);
    rangeBtns.forEach((b) => b.classList.toggle("on", b.textContent === r));
    load(true);
  }

  function rangeLabel() {
    return { "7d": "7 days", "30d": "30 days", "90d": "90 days" }[range] || range;
  }

  function labels() {
    const out = {};
    for (const p of (data && data.phases) || []) out[p.id] = p.label;
    return out;
  }

  function itemRow(it) {
    const lab = labels();
    const isOpen = open.has(it.id);
    const head = el(
      "button",
      { class: "wf-item-head", type: "button", "aria-expanded": isOpen ? "true" : "false", onClick: () => toggle(it.id) },
      el("span", { class: "wf-item-name", text: itemTitle(it) }),
      el("span", { class: "wf-chip s-" + it.status, text: STATUS_WORDS[it.status] || it.status }),
      el("span", { class: "muted wf-item-branch", text: (it.repo ? it.repo + " · " : "") + it.branch }),
      el("span", { class: "wf-item-lead", text: it.lead_to_pr !== null && it.lead_to_pr !== undefined ? fmtDur(it.lead_to_pr) + " to PR" : fmtDur(it.elapsed) + " so far" })
    );
    const totals = Object.assign({}, it.to_pr || {});
    const kids = [head, phaseBar(totals, lab, "wf-item-bar")];
    if (isOpen) kids.push(itemDetail(it));
    return el("div", { class: "wf-item" + (isOpen ? " open" : "") }, ...kids);
  }

  function itemDetail(it) {
    const lab = labels();
    const d = details.get(it.id);
    const facts = [
      "started " + fmtWhen(it.start),
      it.pr_at ? "PR " + fmtWhen(it.pr_at) : null,
      it.merged_at ? "merged " + fmtWhen(it.merged_at) : null,
      it.commits ? it.commits + " commit" + (it.commits === 1 ? "" : "s") + (it.hook_failures ? ", " + it.hook_failures + " failed hooks" : "") : null,
      it.sessions && it.sessions.length ? "window" + (it.sessions.length === 1 ? "" : "s") + ": " + it.sessions.join(", ") : null,
    ].filter(Boolean);
    const after = Object.keys(it.after_pr || {}).length
      ? el("div", { class: "wf-after" }, el("span", { class: "muted", text: "After the PR opened: " }), Object.entries(it.after_pr).map(([ph, s]) => el("span", { class: "wf-key" }, el("i", { class: "wf-swatch ph-" + ph }), (lab[ph] || ph) + " " + fmtDur(s))))
      : null;
    return el(
      "div",
      { class: "wf-item-detail" },
      el("div", { class: "muted", text: facts.join(" · ") }),
      d ? timelineStrip(d.timeline, lab) : el("div", { class: "muted", text: "Loading timeline…" }),
      d ? el("div", { class: "wf-axis muted" }, el("span", { text: fmtWhen(d.timeline.length ? d.timeline[0].start : it.start) }), el("span", { text: fmtWhen(d.timeline.length ? d.timeline[d.timeline.length - 1].end : it.end) })) : null,
      el("div", { class: "wf-item-phases" }, Object.entries(it.to_pr || {}).sort((a, b) => b[1] - a[1]).map(([ph, s]) => el("span", { class: "wf-key" }, el("i", { class: "wf-swatch ph-" + ph }), (lab[ph] || ph) + " " + fmtDur(s)))),
      after,
      it.pr ? el("a", { class: "wf-link", href: it.pr, target: "_blank", rel: "noopener", text: "Open the PR ↗" }) : null
    );
  }

  async function toggle(id) {
    if (open.has(id)) open.delete(id);
    else open.add(id);
    draw();
    if (open.has(id) && !details.has(id)) {
      try {
        const d = await api.request("/api/workflow/item?id=" + encodeURIComponent(id));
        if (disposed) return;
        details.set(id, d && d.timeline ? d : { timeline: [] });
      } catch (err) {
        api.log.error("item", err);
        details.set(id, { timeline: [] });
      }
      draw();
    }
  }

  function draw() {
    if (disposed || !data) return;
    const lab = labels();
    const items = data.items || [];
    if (!items.length) {
      body.replaceChildren(
        el(
          "div",
          { class: "wf-card wf-empty" },
          el("h4", { text: "Nothing recorded yet" }),
          el(
            "p",
            { class: "muted", text: "The optimizer records every session's phase from the moment it is installed" + (data.tracking_since ? " (recording since " + fmtWhen(data.tracking_since) + ")" : "") + ". Work a ticket through to a PR and the breakdown appears here: agent time, your turns, pre-commit hooks, handoffs and review." }
          )
        )
      );
      return;
    }
    const stats = view === "after_pr" ? data.after_pr : data.to_pr;
    const totals = {};
    for (const r of data.to_pr || []) totals[r.phase] = r.total_s;
    const present = new Set([...(data.to_pr || []), ...(data.after_pr || [])].map((r) => r.phase));
    const basisNote = data.basis === "shipped" ? "Over the " + (data.kpi.prs_opened || 0) + " PR" + (data.kpi.prs_opened === 1 ? "" : "s") + " opened in the last " + rangeLabel() + ", from first work to the PR." : "No PR opened in the last " + rangeLabel() + " — so far, across every item in progress.";
    const viewBtns = [
      ["to_pr", "Until the PR"],
      ["after_pr", "After the PR"],
    ].map(([v, t]) =>
      el("button", {
        class: "wf-btn" + (view === v ? " on" : ""),
        type: "button",
        text: t,
        onClick: () => {
          view = v;
          api.storage.set("view", v);
          draw();
        },
      })
    );
    const parts = [
      kpis(data, rangeLabel()),
      el("div", { class: "wf-card" }, el("h4", { text: "Where time to PR goes" }), el("p", { class: "muted", text: basisNote }), phaseBar(totals, lab, "wf-big-bar"), groupLine(data.to_pr_groups), legend(data.phases || [], present)),
      data.advice && data.advice.length ? el("div", { class: "wf-card" }, el("h4", { text: "Bottlenecks — what to change" }), adviceList(data.advice)) : null,
      el("div", { class: "wf-card" }, el("div", { class: "wf-card-head" }, el("h4", { text: "Phases" }), el("div", { class: "wf-seg-btns" }, ...viewBtns)), phaseTable(stats)),
      trendChart(data.trend || []),
      el("div", { class: "wf-card" }, el("h4", { text: "Work items (" + items.length + ")" }), el("div", { class: "wf-items" }, items.map(itemRow))),
    ];
    // replaceChildren would print a skipped (null) card as the text "null".
    body.replaceChildren(...parts.filter(Boolean));
  }

  async function load(manual) {
    if (disposed) return;
    status.textContent = manual ? "refreshing…" : "";
    try {
      const r = await api.request("/api/workflow/summary?range=" + encodeURIComponent(range));
      if (disposed) return;
      if (r && r.error) throw new Error(r.error);
      data = r;
      details.clear();
      draw();
      // Re-fetch the timelines of the items left open (a snapshot of the
      // set: toggle() deletes and re-adds while this walks it).
      for (const id of [...open]) {
        if ((data.items || []).some((x) => x.id === id)) {
          open.delete(id);
          void toggle(id);
        }
      }
      status.textContent = "updated " + fmtWhen(data.now).split(" ").pop();
    } catch (err) {
      api.log.error("summary failed", err);
      status.textContent = "could not load";
      if (!data) body.replaceChildren(el("p", { class: "muted", text: "Could not reach the server." }));
    }
  }

  load(false);
  timer = setInterval(() => load(false), REFRESH_MS);
  if (api.events && typeof api.events.subscribe === "function") {
    let pending = null;
    const soon = () => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = null;
        load(false);
      }, 2000);
    };
    try {
      unsubs.push(api.events.subscribe("session.stage_changed", soon));
      unsubs.push(api.events.subscribe("session.pr_state_changed", soon));
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
