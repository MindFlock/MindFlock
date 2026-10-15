/** The Workflow Optimizer dashboard: one renderer for the dialog and the pane.
 *
 * GET /api/workflow/summary?range= → the headline (median time to commit —
 * a commit finishes a unit of work), where that time goes (phases, grouped by
 * who is the bottleneck), what to change, the handoffs after the commit, the
 * weekly trend and every unit; a unit expands into its own timeline
 * (GET /api/workflow/item?id=). Refreshes every minute while mounted
 * and soon after a stage change or a merge. Range and the phase-table view
 * are remembered per browser in api.storage.
 *
 * "Include my time" (off by default) is the extension's one server-side
 * setting (GET/POST /api/workflow/settings). Off, the server cuts the
 * person's own phases — waiting on your answer, your turn, idle while away —
 * out of every number, so only agent, tooling and limit time counts toward
 * the commit. Flipping it saves the setting and reloads at once with an
 * explicit include_me= override, keeping the old numbers on screen until
 * the new ones land (no blank flash, no layout jump).
 *
 * The legend's chips are switches too: click a phase to hide it (the
 * `hidden` setting, same endpoint). A hidden phase is cut out of every
 * number the way your own time is — the medians are then the time of what
 * is left showing — and its chip stays, struck through, to bring it back.
 * The handoffs after the commit (pushing, the PR, review) sit in the same
 * legend, off by default: switching one on (`count_after`) adds its waits
 * to the headline.
 *
 * Every redraw keeps the scroll position and the focused control, so a
 * click never bounces the panel back to the top. */

const REFRESH_MS = 60000;
const RANGES = ["7d", "30d", "90d"];
const HINT_OFF = "Off: assumes you respond instantly — only agent, tooling and limit time count.";
const HINT_ON = "On: your answers, your turns and time away count too.";
// The handoffs after a commit (analysis.AFTER_PHASES): off by default; on,
// they count toward the headline (`count_after`).
const AFTER_PHASES = ["to_push", "to_pr", "review"];
const STATUS_WORDS = {
  committed: "committed",
  in_progress: "in progress",
  abandoned: "abandoned",
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
  // With your time left out the strip is compressed (no holes where you
  // were): `at` is each piece's real start, `start` its place on the strip.
  const strip = el("div", { class: "wf-strip" });
  if (!timeline || !timeline.length) return strip;
  const a = timeline[0].start;
  const span = Math.max(1, timeline[timeline.length - 1].end - a);
  for (const sg of timeline) {
    const piece = el("span", { class: "wf-seg ph-" + sg.ph, title: (labels[sg.ph] || sg.ph) + " — " + fmtDur(sg.end - sg.start) + " from " + fmtWhen(sg.at !== undefined ? sg.at : sg.start) });
    piece.style.left = ((100 * (sg.start - a)) / span).toFixed(3) + "%";
    piece.style.width = Math.max(0.15, (100 * (sg.end - sg.start)) / span).toFixed(3) + "%";
    strip.appendChild(piece);
  }
  return strip;
}

/** One legend chip, a switch: `on` counts the phase, a click flips it. */
function chip(p, on, onClick, offTitle, onTitle) {
  return el(
    "button",
    {
      class: "wf-key wf-key-btn" + (on ? "" : " off"),
      type: "button",
      "aria-pressed": on ? "true" : "false",
      "data-wf-key": "ph:" + p.id,
      title: (on ? onTitle : offTitle) + ": " + p.hint,
      onClick: () => onClick(p.id),
    },
    el("i", { class: "wf-swatch ph-" + p.id }),
    p.label
  );
}

/** The time-to-commit legend: the phases in `present` plus the hidden ones
 * (click to hide / show), then the handoffs after the commit in `after`
 * (click to count / stop counting them; on only when in `counted`). */
function legend(phases, present, hidden, onHide, after, counted, onCount) {
  const before = phases.filter((p) => !AFTER_PHASES.includes(p.id) && (present.has(p.id) || hidden.has(p.id)));
  const later = phases.filter((p) => AFTER_PHASES.includes(p.id) && after.has(p.id));
  if (!before.length && !later.length) return null;
  return el(
    "div",
    { class: "wf-legend" },
    before.map((p) => chip(p, !hidden.has(p.id), onHide, "Hidden — click to show it again", "Click to hide it and leave its time out")),
    later.length ? el("span", { class: "muted wf-legend-sep", text: "After the commit:" }) : null,
    later.map((p) => chip(p, counted.has(p.id), onCount, "Not counted — click to add its time to the total", "Counted — click to leave it out again"))
  );
}

/** " + pushing + PR open" for the handoffs counted in the headline. */
function afterWords(data) {
  const lab = {};
  for (const p of data.phases || []) lab[p.id] = p.label;
  const on = (data.count_after || []).map((id) => lab[id] || id);
  return on.length ? ", plus " + on.join(", ").toLowerCase() : "";
}

function kpis(data, rangeLabel) {
  const k = data.kpi || {};
  const mine = data.include_me !== false;
  const tile = (num, label, sub) => el("div", { class: "wf-kpi" }, el("div", { class: "wf-kpi-num", text: num }), el("div", { class: "wf-kpi-label", text: label }), sub ? el("div", { class: "muted wf-kpi-sub", text: sub }) : null);
  return el(
    "div",
    { class: "wf-kpis" },
    tile(fmtDur(k.median_to_commit), (mine ? "median time to commit, including your time" : "median time to commit, excluding your time") + afterWords(data), k.p90_to_commit !== null && k.p90_to_commit !== undefined ? "p90 " + fmtDur(k.p90_to_commit) : "nothing committed in the last " + rangeLabel),
    tile(fmtDur(k.median_active_to_commit), "median active time to commit", mine ? "without idle-while-away and untracked time" : "without untracked time"),
    tile(String(k.committed || 0), "units committed", (k.estimated ? k.estimated + " estimated · " : "") + (k.items || 0) + " units"),
    tile(String(k.in_progress || 0), "in progress", "not committed yet")
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
  const vals = (trend || []).map((w) => w.median_to_commit);
  if (trend.length < 2 || !vals.some((v) => v !== null && v !== undefined)) return null;
  const max = Math.max(...vals.filter((v) => v !== null && v !== undefined), 1);
  return el(
    "div",
    { class: "wf-card" },
    el("h4", { text: "Median time to commit, by week" }),
    el(
      "div",
      { class: "wf-trend" },
      trend.map((w) => {
        const h = w.median_to_commit ? Math.max(4, (100 * w.median_to_commit) / max) : 0;
        const bar = el("span", { class: "wf-trend-bar" + (w.median_to_commit ? "" : " empty") });
        bar.style.height = h.toFixed(1) + "%";
        return el(
          "div",
          { class: "wf-trend-col", title: "Week ending " + fmtWhen(w.end) + ": " + (w.median_to_commit ? fmtDur(w.median_to_commit) + " median over " + w.items + " unit" + (w.items === 1 ? "" : "s") : "no commits") },
          el("div", { class: "wf-trend-plot" }, bar),
          el("div", { class: "muted wf-trend-label", text: w.median_to_commit ? fmtDur(w.median_to_commit) : "–" })
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
  let view = api.storage.get("view", "to_commit");
  if (view !== "to_commit" && view !== "after_commit") view = "to_commit";
  // null until the server says (the saved setting); then what this view shows.
  let includeMe = null;
  let hidden = null;
  let counted = null;
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
  const meBox = el("input", { type: "checkbox", class: "wf-switch-input", disabled: true, onChange: () => setIncludeMe(meBox.checked) });
  const meHint = el("div", { class: "muted wf-me-hint", text: HINT_OFF });
  const meSwitch = el("label", { class: "wf-switch", title: HINT_OFF }, meBox, el("span", { class: "wf-switch-track", "aria-hidden": "true" }), el("span", { text: "Include my time" }));
  const top = el("div", { class: "wf-top" }, el("div", { class: "wf-head" }, el("div", { class: "wf-head-title", text: "Where the time goes, work → commit" }), el("div", { class: "muted", text: "Every unit of work (from the first real work to the commit that finishes it), split by what was holding it." })), el("div", { class: "wf-controls" }, meSwitch, ...rangeBtns, refreshBtn, windowBtn, status), meHint);
  const body = el("div", { class: "wf-body" }, el("p", { class: "muted", text: "Loading the workflow timeline…" }));
  const wrap = el("div", { class: "wf-wrap" }, top, body);
  root.appendChild(wrap);

  function setRange(r) {
    range = r;
    api.storage.set("range", r);
    rangeBtns.forEach((b) => b.classList.toggle("on", b.textContent === r));
    load(true);
  }

  function showIncludeMe() {
    meBox.disabled = includeMe === null;
    meBox.checked = includeMe === true;
    const hint = includeMe ? HINT_ON : HINT_OFF;
    meHint.textContent = hint;
    meSwitch.title = hint;
  }

  async function setIncludeMe(on) {
    includeMe = !!on;
    showIncludeMe();
    details.clear();
    void load(true);
    try {
      await api.request("/api/workflow/settings", { json: { include_me: includeMe } });
    } catch (err) {
      api.log.error("settings", err);
      status.textContent = "could not save the setting";
    }
  }

  async function toggleHidden(id) {
    hidden = new Set(hidden || []);
    if (hidden.has(id)) hidden.delete(id);
    else hidden.add(id);
    details.clear();
    void load(true);
    try {
      await api.request("/api/workflow/settings", { json: { hidden: [...hidden] } });
    } catch (err) {
      api.log.error("settings", err);
      status.textContent = "could not save the setting";
    }
  }

  async function toggleCounted(id) {
    counted = new Set(counted || []);
    if (counted.has(id)) counted.delete(id);
    else counted.add(id);
    details.clear();
    void load(true);
    try {
      await api.request("/api/workflow/settings", { json: { count_after: [...counted] } });
    } catch (err) {
      api.log.error("settings", err);
      status.textContent = "could not save the setting";
    }
  }

  function meParam() {
    return (
      (includeMe === null ? "" : "&include_me=" + (includeMe ? "1" : "0")) +
      (hidden === null ? "" : "&hide=" + encodeURIComponent([...hidden].join(","))) +
      (counted === null ? "" : "&count_after=" + encodeURIComponent([...counted].join(",")))
    );
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
      { class: "wf-item-head", type: "button", "aria-expanded": isOpen ? "true" : "false", "data-wf-key": "item:" + it.id, onClick: () => toggle(it.id) },
      el("span", { class: "wf-item-name", text: itemTitle(it) }),
      el("span", { class: "wf-chip s-" + it.status, text: STATUS_WORDS[it.status] || it.status }),
      el("span", { class: "muted wf-item-branch", text: (it.repo ? it.repo + " · " : "") + it.branch }),
      el("span", { class: "wf-item-lead", text: it.lead_to_commit !== null && it.lead_to_commit !== undefined ? fmtDur(it.lead_to_commit) + " to commit" + (it.estimated ? " (est.)" : "") : fmtDur(it.elapsed) + " so far" })
    );
    const totals = Object.assign({}, it.to_commit || {});
    const kids = [head, phaseBar(totals, lab, "wf-item-bar")];
    if (isOpen) kids.push(itemDetail(it));
    return el("div", { class: "wf-item" + (isOpen ? " open" : "") }, ...kids);
  }

  function itemDetail(it) {
    const lab = labels();
    const d = details.get(it.id);
    const facts = [
      "started " + fmtWhen(it.start),
      it.commit_at ? (it.estimated ? "committed ~" : "committed ") + fmtWhen(it.commit_at) : null,
      it.estimated ? "commit time estimated (recorded before commits were tracked)" : null,
      it.hook_runs ? it.hook_runs + " hook run" + (it.hook_runs === 1 ? "" : "s") + (it.hook_failures ? ", " + it.hook_failures + " failed" : "") : null,
      it.sessions && it.sessions.length ? "window" + (it.sessions.length === 1 ? "" : "s") + ": " + it.sessions.join(", ") : null,
      it.your_time_s ? fmtDur(it.your_time_s) + " of your time left out" : null,
    ].filter(Boolean);
    const compressed = !!(d && d.timeline.length && d.timeline[0].at !== undefined);
    const after = Object.keys(it.after_commit || {}).length
      ? el("div", { class: "wf-after" }, el("span", { class: "muted", text: "After the commit: " }), Object.entries(it.after_commit).map(([ph, s]) => el("span", { class: "wf-key" }, el("i", { class: "wf-swatch ph-" + ph }), (lab[ph] || ph) + " " + fmtDur(s))))
      : null;
    return el(
      "div",
      { class: "wf-item-detail" },
      el("div", { class: "muted", text: facts.join(" · ") }),
      d ? timelineStrip(d.timeline, lab) : el("div", { class: "muted", text: "Loading timeline…" }),
      d ? el("div", { class: "wf-axis muted" }, el("span", { text: fmtWhen(d.timeline.length ? d.timeline[0].start : it.start) }), compressed ? el("span", { text: "your time cut out" }) : null, el("span", { text: fmtWhen(compressed || !d.timeline.length ? it.end : d.timeline[d.timeline.length - 1].end) })) : null,
      el("div", { class: "wf-item-phases" }, Object.entries(it.to_commit || {}).sort((a, b) => b[1] - a[1]).map(([ph, s]) => el("span", { class: "wf-key" }, el("i", { class: "wf-swatch ph-" + ph }), (lab[ph] || ph) + " " + fmtDur(s)))),
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
        const d = await api.request("/api/workflow/item?id=" + encodeURIComponent(id) + meParam());
        if (disposed) return;
        details.set(id, d && d.timeline ? d : { timeline: [] });
      } catch (err) {
        api.log.error("item", err);
        details.set(id, { timeline: [] });
      }
      draw();
    }
  }

  /** Swap the body's content in place: the scroll position and the focused
   * control (matched by data-wf-key — the old node is gone) survive it. */
  function swap(parts) {
    const y = wrap.scrollTop;
    const a = document.activeElement;
    const key = a && body.contains(a) && a.getAttribute("data-wf-key");
    body.replaceChildren(...parts);
    wrap.scrollTop = y;
    if (key) {
      const n = [...body.querySelectorAll("[data-wf-key]")].find((x) => x.getAttribute("data-wf-key") === key);
      if (n) n.focus({ preventScroll: true });
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
            { class: "muted", text: "The optimizer records every session's phase from the moment it is installed" + (data.tracking_since ? " (recording since " + fmtWhen(data.tracking_since) + ")" : "") + ". Work something through to a commit and the breakdown appears here: agent time, pre-commit hooks and limits" + (data.include_me === false ? "." : ", and your turns.") }
          )
        )
      );
      return;
    }
    const stats = view === "after_commit" ? data.after_commit : data.to_commit;
    const totals = {};
    for (const r of data.to_commit || []) totals[r.phase] = r.total_s;
    const present = new Set((data.to_commit || []).map((r) => r.phase));
    const off = new Set([...(hidden || [])].filter((p) => !AFTER_PHASES.includes(p)));
    const on = new Set(data.count_after || []);
    const afterPresent = new Set([...(data.after_commit || []).map((r) => r.phase), ...on]);
    const n = data.kpi.committed || 0;
    const basisNote =
      data.basis === "committed"
        ? "Over the " + n + " unit" + (n === 1 ? "" : "s") + " committed in the last " + rangeLabel() + ", from first work to the commit." + (data.kpi.estimated ? " " + data.kpi.estimated + " of the commit times are estimated (recorded before commits were tracked)." : "")
        : "Nothing committed in the last " + rangeLabel() + " — so far, across every unit in progress.";
    // Pushing, the PR and review come after the commit: in this total only
    // when switched on below.
    const afterNote = on.size
      ? " Also counted, after the commit: " + [...on].map((p) => (lab[p] || p).toLowerCase()).join(", ") + "."
      : " Pushing, the PR and review come after the commit and aren't counted — switch one on below to add it.";
    const meNote =
      data.include_me === false
        ? "Your own time is left out" + (data.excluded && data.excluded.total_s ? " (" + fmtDur(data.excluded.total_s) + " of answers, turns and time away)" : "") + ", as if you answered instantly." + afterNote
        : "Includes your own time: answering questions, your turns and time away." + afterNote;
    const hiddenNote = off.size
      ? " Hidden (click a struck-out phase to bring it back): " + [...off].map((p) => lab[p] || p).join(", ") + (data.hidden && data.hidden.total_s ? " — " + fmtDur(data.hidden.total_s) + " left out." : ".")
      : " Click a phase below to hide it.";
    const viewBtns = [
      ["to_commit", "Until the commit"],
      ["after_commit", "After the commit"],
    ].map(([v, t]) =>
      el("button", {
        class: "wf-btn" + (view === v ? " on" : ""),
        type: "button",
        text: t,
        "data-wf-key": "view:" + v,
        onClick: () => {
          view = v;
          api.storage.set("view", v);
          draw();
        },
      })
    );
    const parts = [
      kpis(data, rangeLabel()),
      el("div", { class: "wf-card" }, el("h4", { text: "Where time to commit goes" }), el("p", { class: "muted", text: basisNote + " " + meNote + hiddenNote }), phaseBar(totals, lab, "wf-big-bar"), groupLine(data.to_commit_groups), legend(data.phases || [], present, off, toggleHidden, afterPresent, on, toggleCounted)),
      data.advice && data.advice.length ? el("div", { class: "wf-card" }, el("h4", { text: "Bottlenecks — what to change" }), adviceList(data.advice)) : null,
      el("div", { class: "wf-card" }, el("div", { class: "wf-card-head" }, el("h4", { text: "Phases" }), el("div", { class: "wf-seg-btns" }, ...viewBtns)), phaseTable(stats)),
      trendChart(data.trend || []),
      el("div", { class: "wf-card" }, el("h4", { text: "Units of work (" + items.length + ")" }), el("div", { class: "wf-items" }, items.map(itemRow))),
    ];
    // replaceChildren would print a skipped (null) card as the text "null".
    swap(parts.filter(Boolean));
  }

  async function load(manual) {
    if (disposed) return;
    status.textContent = manual ? "refreshing…" : "";
    try {
      const asked = meParam();
      const r = await api.request("/api/workflow/summary?range=" + encodeURIComponent(range) + asked);
      if (disposed) return;
      if (r && r.error) throw new Error(r.error);
      // A flip (your time, a phase) while this was in flight: its own load
      // is coming.
      if (asked !== meParam()) return;
      data = r;
      if (typeof r.include_me === "boolean" && includeMe === null) {
        includeMe = r.include_me;
        showIncludeMe();
      }
      if (Array.isArray(r.hide) && hidden === null) hidden = new Set(r.hide);
      if (Array.isArray(r.count_after) && counted === null) counted = new Set(r.count_after);
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
