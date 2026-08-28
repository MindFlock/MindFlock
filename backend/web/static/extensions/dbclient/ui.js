/** DOM helpers shared by the dbclient surfaces: a tiny element builder, the
 * monochrome inline-SVG icon set, the confirm bar (the one place a destructive
 * or generated-SQL step is shown before it runs), the in-pane read-only
 * overlay, notices, and a few formatters.
 *
 * Everything here builds plain DOM — no framework, no template strings with
 * user data — because the host hands each surface a bare keep-alive <div> and
 * expects the extension to own what goes inside it. Icons are inline SVG with
 * currentColor strokes so they follow the theme like the app's own chrome. */

/** el("div", {class: "x", onClick: fn, dataset: {…}}, child, "text", [more]).
 * Property-ish keys (value, disabled, checked, hidden, …) are assigned as
 * properties; on<Event> keys become listeners; everything else is an
 * attribute. null/false/undefined children are skipped, arrays flattened. */
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, val] of Object.entries(attrs)) {
      if (val === undefined || val === null || val === false) continue;
      if (key === "class" || key === "className") node.className = val;
      else if (key === "text") node.textContent = val;
      else if (key === "dataset") Object.assign(node.dataset, val);
      else if (key === "style" && typeof val === "object") Object.assign(node.style, val);
      else if (key === "for") node.htmlFor = val;
      else if (/^on[A-Z]/.test(key)) node.addEventListener(key.slice(2).toLowerCase(), val);
      else if (key.startsWith("aria-") || key.startsWith("data-")) node.setAttribute(key, val);
      else if (key in node) node[key] = val;
      else node.setAttribute(key, val === true ? "" : val);
    }
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(node, child);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

// --- Icons -----------------------------------------------------------------
// 24×24 viewBox, stroke-only, currentColor. Kept deliberately plain: these sit
// at 12–14px next to text and have to read at that size.
const ICONS = {
  database:
    '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.66 3.58 3 8 3s8-1.34 8-3V6"/><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/>',
  table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/>',
  view: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  schema: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  server:
    '<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/>',
  "chevron-right": '<path d="M9 6l6 6-6 6"/>',
  "chevron-down": '<path d="M6 9l6 6 6-6"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  play: '<path d="M7 5v14l11-7z"/>',
  "play-all": '<path d="M4 5v14l8-7z"/><path d="M13 5v14l8-7z"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M4 19h16"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  null: '<circle cx="12" cy="12" r="8"/><path d="M6.5 17.5l11-11"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M15 5l3 3"/>',
  "arrow-left": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
  save: '<path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4-2v-4z"/>',
  check: '<path d="M5 12l5 5L20 7"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
  edit: '<path d="M4 20h4l11-11-4-4L4 16z"/><path d="M13 7l4 4"/>',
  history: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  code: '<path d="M8 8l-4 4 4 4M16 8l4 4-4 4M14 5l-4 14"/>',
  "sort-asc": '<path d="M12 19V5M6 11l6-6 6 6"/>',
  "sort-desc": '<path d="M12 5v14M6 13l6 6 6-6"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
  columns: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M10 4v16M16 4v16"/>',
};

const SVG_NS = "http://www.w3.org/2000/svg";

/** A monochrome icon element. Unknown names render an empty box rather than
 * throwing — a missing glyph must never take a surface down. */
export function svgIcon(name, extraClass) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "dbc-icon" + (extraClass ? " " + extraClass : ""));
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

/** A toolbar/action button. kind: "" | "primary" | "danger" | "icon" (icon-only,
 * needs a title — the label becomes the accessible name). */
export function button(label, opts = {}) {
  const cls = ["dbc-btn"];
  if (opts.kind) cls.push(opts.kind);
  if (opts.class) cls.push(opts.class);
  const b = el("button", {
    type: "button",
    class: cls.join(" "),
    title: opts.title,
    disabled: !!opts.disabled,
    onClick: opts.onClick,
    "aria-label": opts.kind === "icon" ? label : undefined,
  });
  if (opts.icon) b.appendChild(svgIcon(opts.icon));
  if (opts.kind !== "icon" && label) b.appendChild(el("span", { text: label }));
  return b;
}

export function option(value, label, selected) {
  const o = el("option", { value, text: label === undefined ? value : label });
  if (selected) o.selected = true;
  return o;
}

// --- Confirm bar -----------------------------------------------------------

/** container → resolver of the bar currently shown there, so a second confirm
 * on the same container settles (false) the first instead of stacking. */
const pendingConfirms = new WeakMap();

/** Show a confirm bar inside `container`; resolves true on confirm, false on
 * cancel/dismiss. opts: {title, body (Node | string), pre (string shown in a
 * <pre>), confirmLabel, cancelLabel, danger}. */
export function confirmBar(container, opts = {}) {
  dismissConfirm(container);
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      bar.remove();
      if (pendingConfirms.get(container) === done) pendingConfirms.delete(container);
      resolve(v);
    };
    const confirmBtn = button(opts.confirmLabel || "Confirm", {
      kind: opts.danger ? "danger" : "primary",
      onClick: () => done(true),
    });
    const bar = el(
      "div",
      { class: "dbc-confirm" + (opts.danger ? " danger" : ""), role: "alertdialog" },
      el("div", { class: "dbc-confirm-head" }, svgIcon("alert"), el("span", { text: opts.title || "Confirm" })),
      opts.body instanceof Node ? opts.body : opts.body ? el("p", { class: "dbc-confirm-body", text: opts.body }) : null,
      opts.pre ? el("pre", { class: "dbc-confirm-pre", text: opts.pre }) : null,
      el(
        "div",
        { class: "dbc-confirm-actions" },
        button(opts.cancelLabel || "Cancel", { onClick: () => done(false) }),
        confirmBtn
      )
    );
    bar.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        done(false);
      }
    });
    pendingConfirms.set(container, done);
    container.appendChild(bar);
    confirmBtn.focus();
  });
}

/** Settle (false) and remove any confirm bar shown in `container`. */
export function dismissConfirm(container) {
  const done = pendingConfirms.get(container);
  if (done) done(false);
}

// --- Notices ---------------------------------------------------------------

/** One inline message slot: {el, set(msg, kind), clear()}. kind: "error" |
 * "info" | "ok" | "warn". */
export function makeNotice(extraClass) {
  const node = el("div", { class: "dbc-notice" + (extraClass ? " " + extraClass : ""), hidden: true, role: "status" });
  return {
    el: node,
    set(msg, kind) {
      node.className = "dbc-notice " + (kind || "info") + (extraClass ? " " + extraClass : "");
      node.textContent = msg;
      node.hidden = !msg;
    },
    clear() {
      node.hidden = true;
      node.textContent = "";
    },
  };
}

// --- Read-only overlay -----------------------------------------------------

/** A read-only overlay covering `root` (which must be position: relative) —
 * used for a truncated cell's head text. Returns a close function; Escape and
 * the close button also close it. */
export function showOverlay(root, opts = {}) {
  const close = () => {
    overlay.remove();
  };
  const overlay = el(
    "div",
    { class: "dbc-overlay", tabIndex: -1 },
    el(
      "div",
      { class: "dbc-overlay-head" },
      el("span", { class: "dbc-overlay-title", text: opts.title || "Value" }),
      opts.note ? el("span", { class: "dbc-overlay-note", text: opts.note }) : null,
      button("Copy", {
        icon: "copy",
        onClick: () => copyText(opts.text || ""),
      }),
      button("Close", { kind: "icon", icon: "close", title: "Close", onClick: close })
    ),
    el("pre", { class: "dbc-overlay-pre", text: opts.text || "" })
  );
  overlay.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
    }
  });
  root.appendChild(overlay);
  overlay.focus();
  return close;
}

/** Clipboard write with the execCommand fallback for plain-http origins,
 * where navigator.clipboard is undefined. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {
    /* fall through to the legacy path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

// --- Misc ------------------------------------------------------------------

export function debounce(fn, ms) {
  let t = null;
  const wrapped = (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => {
      t = null;
      fn(...args);
    }, ms);
  };
  wrapped.cancel = () => {
    if (t) clearTimeout(t);
    t = null;
  };
  return wrapped;
}

export function errMsg(err) {
  return String((err && err.message) || err || "unknown error");
}

export function fmtNum(n) {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : String(n);
}

export function fmtBytes(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "?";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

/** Identifier quoting per engine — for the SQL the explorer pre-fills into a
 * new query pane. The server validates and quotes everything it executes on
 * its own; this only has to produce text a human would have typed. */
export function quoteIdent(engine, name) {
  const s = String(name);
  if (engine === "mysql") return "`" + s.replace(/`/g, "``") + "`";
  return '"' + s.replace(/"/g, '""') + '"';
}

export function qualifiedName(engine, schema, table) {
  const t = quoteIdent(engine, table);
  return schema ? quoteIdent(engine, schema) + "." + t : t;
}
