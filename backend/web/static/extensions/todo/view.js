/** todo/view.js — the To-do window (surface "list", a grid pane).
 *
 * Two tabs over one store (the shared contract, GET /api/todo):
 *
 *   To do    a quick-add row, then "Needs you" (open manual_test / needs_you
 *            items grouped by session — what agents are waiting on), then the
 *            rest, with a tier heading only when more than one tier is used.
 *   History  GET /api/todo/history, grouped by day; Restore / Forget.
 *
 * Rows: checkbox, text, quiet chips (kind, session — one click focuses it —,
 * source, due, details marker). A click on the row expands it: the details
 * read-friendly (links, fenced code with Copy), the editors (details,
 * priority, due, link) and Delete. Done and Delete leave an inline
 * "Done · Undo" strip for 6 s — never window.confirm/alert/prompt (Electron
 * has none).
 *
 * Order: drag a row by its grip (pointer events, an insertion line), or
 * Alt+Up / Alt+Down. A drop inside another tier's run re-prioritizes the item
 * (PATCH priority, then POST order).
 *
 * Live: refetch on `addon.todo.changed` and every 30 s (the Assistant's agent
 * edits the file directly). A redraw keeps the scroll position, the expanded
 * rows and the focus, and is DEFERRED while a text field inside the list has
 * focus, so nothing being typed is ever lost. Every change is optimistic; a
 * failure shows an error line and refetches. */

import {
  applyOrder,
  applyTier,
  buildSections,
  closedAt,
  clock,
  formatDue,
  groupHistoryByDay,
  KIND_LABELS,
  openItems,
  orderAfterDrop,
  orderAfterStep,
  paneTitle,
  parseDue,
  SOURCE_LABELS,
  sortByTier,
  tierOf,
  TIER_LABELS,
  toDueString,
} from "./model.js";
import { autoGrow, call, el, errText, focusedSession, icon, isTextEntry, prioritySwitch, renderDetails, sessionTag } from "./ui.js";

const REFRESH_MS = 30000;
const UNDO_MS = 6000;
const ERROR_MS = 10000;
const FOCUS_POLL_MS = 1500;
const HISTORY_LIMIT = 200;

const EMPTY_LINE = "Things you need to do by hand. Agents add manual test steps here.";

/** Live windows, so the code-backed `todo.history` command can switch an
 * already-open pane (opening it again only reveals it — no re-render). */
const live = new Set();
let wantTab = null;

/** Ask every open To-do window (and the next one to mount) for a tab. */
export function requestTab(tab) {
  wantTab = live.size ? null : tab;
  for (const v of live) v.showTab(tab);
}

export function renderList(api, host) {
  const root = host.el;
  root.classList.add("mfx-todo-root");

  // --- state -----------------------------------------------------------------
  let todos = [];
  /** Ids checked off in this view, still listed crossed out (see leaveList). */
  const lingering = new Set();
  let loaded = false;
  let loadFailed = "";
  let tab = (host.ctx && host.ctx.tab) === "history" || wantTab === "history" ? "history" : "todo";
  wantTab = null;
  let q = "";
  let history = null;
  let historyFailed = "";
  let historyReq = 0;
  const expanded = new Set();
  const hExpanded = new Set();
  let editText = null;
  let editDetails = null;
  let forgetAsk = null;
  const goneSessions = new Set();
  let lastSession = "";
  let tagOn = api.storage.get("tagSession", true) !== false;
  let title = "";
  let disposed = false;
  let inflight = 0;
  let mutSeq = 0;
  let pendingDraw = false;
  let drag = null;
  let undo = null;
  const timers = new Set();
  const unsubs = [];

  const later = (fn, ms) => {
    const t = setTimeout(() => {
      timers.delete(t);
      fn();
    }, ms);
    timers.add(t);
    return t;
  };
  const cancel = (t) => {
    if (t) {
      clearTimeout(t);
      timers.delete(t);
    }
  };

  // --- chrome (built once; never redrawn, so typed text survives) -------------
  const tabTodo = el("button", { class: "mfx-todo-tab", type: "button", role: "tab", "data-key": "tab:todo", onClick: () => showTab("todo") });
  const tabHist = el("button", { class: "mfx-todo-tab", type: "button", role: "tab", "data-key": "tab:history", onClick: () => showTab("history") }, "History");
  const filter = el("input", {
    class: "mfx-todo-filter-input",
    type: "search",
    placeholder: "Filter",
    "aria-label": "Filter by text, details or session",
    onInput: () => {
      q = filter.value;
      if (tab === "history") scheduleHistory(250);
      else draw();
    },
    onKeydown: (e) => {
      if (e.key === "Escape" && filter.value) {
        e.preventDefault();
        e.stopPropagation();
        filter.value = "";
        q = "";
        if (tab === "history") scheduleHistory(0);
        else draw();
      }
    },
  });
  const head = el(
    "div",
    { class: "mfx-todo-head" },
    el("div", { class: "mfx-todo-tabs", role: "tablist", "aria-label": "To-do views" }, tabTodo, tabHist),
    el("label", { class: "mfx-todo-filter" }, icon("search"), filter)
  );

  const addInput = el("input", {
    class: "mfx-todo-add-input",
    type: "text",
    placeholder: "Add a to-do…",
    "aria-label": "New to-do, Enter to add",
    maxLength: 500,
    onKeydown: (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        quickAdd();
      } else if (e.key === "Escape") {
        e.stopPropagation();
        if (addInput.value) addInput.value = "";
        else addInput.blur();
      }
    },
    onFocus: () => tag.refresh(),
  });
  const tag = sessionTag(
    () => ({ session: currentSession(), on: tagOn }),
    () => {
      tagOn = !tagOn;
      api.storage.set("tagSession", tagOn);
      tag.refresh();
    }
  );
  const addBtn = el("button", { class: "mfx-todo-icon-btn mfx-todo-add-btn", type: "button", "aria-label": "Add", title: "Add (Enter)", onClick: () => quickAdd() }, icon("plus"));
  const moreBtn = el("button", {
    class: "mfx-todo-link-btn",
    type: "button",
    text: "Details…",
    title: "Add with details, priority and a due time",
    onClick: () => {
      try {
        api.ui.openDialog("add", undefined, { text: addInput.value.trim() });
        addInput.value = "";
      } catch (err) {
        api.log.error("open add dialog", err);
      }
    },
  });
  const addRow = el("div", { class: "mfx-todo-add" }, el("div", { class: "mfx-todo-add-box" }, addInput, addBtn), el("div", { class: "mfx-todo-add-meta" }, tag.node, moreBtn));

  const errText_ = el("span", { class: "mfx-todo-error-text" });
  let errTimer = null;
  const errLine = el(
    "div",
    { class: "mfx-todo-error", role: "alert", hidden: true },
    errText_,
    el("button", { class: "mfx-todo-icon-btn", type: "button", "aria-label": "Dismiss", onClick: () => hideError() }, icon("close"))
  );

  const list = el("div", { class: "mfx-todo-list" });
  const dropLine = el("div", { class: "mfx-todo-dropline", hidden: true }, el("span", { class: "mfx-todo-dropline-tier" }));
  const scroll = el("div", { class: "mfx-todo-scroll" }, list, dropLine);

  const undoText = el("span", { class: "mfx-todo-undo-text" });
  const undoBtn = el("button", { class: "mfx-todo-undo-btn", type: "button", text: "Undo", onClick: () => runUndo() });
  const undoStrip = el(
    "div",
    { class: "mfx-todo-undo", role: "status", hidden: true },
    undoText,
    undoBtn,
    el("button", { class: "mfx-todo-icon-btn", type: "button", "aria-label": "Dismiss", onClick: () => hideUndo() }, icon("close"))
  );

  const wrap = el("div", { class: "mfx-todo-wrap" }, head, addRow, errLine, scroll, undoStrip);
  root.replaceChildren(wrap);

  // --- helpers ---------------------------------------------------------------
  function currentSession() {
    const s = focusedSession(api);
    if (s) lastSession = s;
    return s || lastSession;
  }

  function knownSessions() {
    try {
      const all = typeof api.sessions === "function" ? api.sessions() : null;
      if (!Array.isArray(all) || !all.length) return null;
      const titles = all.map((s) => s && s.title).filter((t) => typeof t === "string");
      return titles.length === all.length ? new Set(titles) : null;
    } catch (_) {
      return null;
    }
  }

  function showError(msg) {
    errText_.textContent = msg;
    errLine.hidden = false;
    cancel(errTimer);
    errTimer = later(hideError, ERROR_MS);
  }
  function hideError() {
    errLine.hidden = true;
    cancel(errTimer);
    errTimer = null;
  }

  function showUndo(label, run, action) {
    hideUndo();
    undoText.textContent = label;
    undoBtn.textContent = action || "Undo";
    undoStrip.hidden = false;
    undo = { run, timer: later(hideUndo, UNDO_MS) };
  }
  function hideUndo() {
    if (undo) cancel(undo.timer);
    undo = null;
    undoStrip.hidden = true;
  }
  function runUndo() {
    const u = undo;
    hideUndo();
    if (u) u.run();
  }

  function setTitle() {
    const open = openItems(todos);
    const needs = open.filter((t) => t.kind === "manual_test" || t.kind === "needs_you").length;
    const next = paneTitle(needs);
    if (next !== title) {
      title = next;
      try {
        host.setTitle(next);
      } catch (_) {
        /* host gone */
      }
    }
    // replaceChildren would print a null child as the text "null".
    tabTodo.replaceChildren(...["To do", open.length ? el("span", { class: "mfx-todo-count", text: String(open.length) }) : null].filter(Boolean));
  }

  function find(id) {
    return todos.find((t) => t.id === id);
  }

  /** Optimistic change: apply `local` and redraw now, then run `remote`; a
   * failure shows the error line, and every settle refetches the truth. */
  async function mutate(what, local, remote) {
    mutSeq++;
    inflight++;
    if (local) {
      local();
      setTitle();
      draw();
    }
    try {
      return await remote();
    } catch (err) {
      api.log.error(what, err);
      showError("Could not " + what + ": " + errText(err));
      return undefined;
    } finally {
      inflight--;
      if (inflight === 0) scheduleLoad(250);
    }
  }

  let loadTimer = null;
  function scheduleLoad(ms) {
    cancel(loadTimer);
    loadTimer = later(() => {
      loadTimer = null;
      void load();
    }, ms);
  }

  async function load() {
    if (disposed) return;
    const seq = mutSeq;
    let r;
    try {
      r = await call(api, "/api/todo");
    } catch (err) {
      if (disposed) return;
      api.log.error("load", err);
      if (!loaded) {
        loadFailed = errText(err);
        draw();
      } else showError("Could not refresh the list: " + errText(err));
      return;
    }
    if (disposed) return;
    // A change made while this was in flight: its own settle refetches.
    if (inflight > 0 || seq !== mutSeq) return;
    todos = Array.isArray(r && r.todos) ? r.todos.filter((t) => t && typeof t === "object" && t.id) : [];
    loaded = true;
    loadFailed = "";
    setTitle();
    draw();
  }

  let histTimer = null;
  function scheduleHistory(ms) {
    cancel(histTimer);
    histTimer = later(() => {
      histTimer = null;
      void loadHistory();
    }, ms);
  }

  async function loadHistory() {
    if (disposed) return;
    const req = ++historyReq;
    let r;
    try {
      r = await call(api, "/api/todo/history?limit=" + HISTORY_LIMIT + (q.trim() ? "&q=" + encodeURIComponent(q.trim()) : ""));
    } catch (err) {
      if (disposed || req !== historyReq) return;
      api.log.error("history", err);
      if (history === null) historyFailed = errText(err);
      else showError("Could not load history: " + errText(err));
      if (tab === "history") draw();
      return;
    }
    if (disposed || req !== historyReq) return;
    history = Array.isArray(r && r.history) ? r.history.filter((h) => h && typeof h === "object") : [];
    historyFailed = "";
    if (tab === "history") draw();
  }

  function showTab(next) {
    if (next !== "todo" && next !== "history") return;
    const changed = tab !== next;
    if (changed) lingering.clear();
    tab = next;
    forgetAsk = null;
    tabTodo.setAttribute("aria-selected", tab === "todo" ? "true" : "false");
    tabHist.setAttribute("aria-selected", tab === "history" ? "true" : "false");
    tabTodo.classList.toggle("is-on", tab === "todo");
    tabHist.classList.toggle("is-on", tab === "history");
    addRow.hidden = tab !== "todo";
    filter.placeholder = tab === "history" ? "Search history" : "Filter";
    if (tab === "history") void loadHistory();
    if (changed) {
      scroll.scrollTop = 0;
      draw(true);
    }
  }

  // --- drawing ---------------------------------------------------------------

  /** Is someone typing in the list (an editor, the due or link field)? */
  function typingInList() {
    const a = document.activeElement;
    return !!a && list.contains(a) && isTextEntry(a);
  }

  /** Rebuild the list body, keeping scroll, focus and expanded rows; deferred
   * while a text field in the list has focus or a drag is in progress. */
  function draw(force) {
    if (disposed) return;
    if (!force && (drag || typingInList())) {
      pendingDraw = true;
      return;
    }
    pendingDraw = false;
    const y = scroll.scrollTop;
    const a = document.activeElement;
    const inList = !!a && list.contains(a);
    const key = inList ? a.getAttribute("data-key") || (a.closest("[data-key]") || {}).getAttribute?.("data-key") : null;
    const rows = [...list.querySelectorAll(".mfx-todo-row, .mfx-todo-hrow")];
    const focusedRow = inList ? a.closest(".mfx-todo-row, .mfx-todo-hrow") : null;
    const rowAt = focusedRow ? rows.indexOf(focusedRow) : -1;

    list.replaceChildren(...(tab === "todo" ? drawTodo() : drawHistory()));
    list.setAttribute("aria-busy", "false");
    scroll.scrollTop = y;

    if (!inList) return;
    const pick = (k) => (k ? [...list.querySelectorAll("[data-key]")].find((n) => n.getAttribute("data-key") === k) : null);
    let target = pick(key);
    if (!target && focusedRow) target = pick(focusedRow.getAttribute("data-key"));
    if (!target && rowAt >= 0) {
      const now = [...list.querySelectorAll(".mfx-todo-row, .mfx-todo-hrow")];
      target = now[Math.min(rowAt, now.length - 1)] || null;
    }
    if (target) target.focus({ preventScroll: true });
    else scroll.focus({ preventScroll: true });
  }

  function emptyState(head, line) {
    return el("div", { class: "mfx-todo-empty" }, el("div", { class: "mfx-todo-empty-head", text: head }), el("p", { class: "mfx-todo-empty-line", text: line }));
  }

  function drawTodo() {
    if (!loaded) {
      if (loadFailed) {
        return [
          el(
            "div",
            { class: "mfx-todo-empty" },
            el("div", { class: "mfx-todo-empty-head", text: "Could not load the list" }),
            el("p", { class: "mfx-todo-empty-line", text: loadFailed }),
            el("button", { class: "mfx-todo-btn", type: "button", text: "Try again", onClick: () => load() })
          ),
        ];
      }
      return [el("p", { class: "mfx-todo-loading", text: "Loading…" })];
    }
    const s = buildSections(todos, q, lingering);
    const known = knownSessions();
    const parts = [];
    if (!s.open && !s.shown) {
      parts.push(emptyState("Nothing to do", EMPTY_LINE));
    } else if (!s.shown) {
      parts.push(emptyState("Nothing matches", "No open item mentions “" + q.trim() + "”. History has the finished ones."));
    }
    if (s.needsYou.length) {
      const n = s.needsYou.reduce((acc, g) => acc + g.items.length, 0);
      parts.push(
        el(
          "section",
          { class: "mfx-todo-section is-needs", "aria-label": "Needs you" },
          el("h3", { class: "mfx-todo-section-head" }, el("span", { text: "Needs you" }), el("span", { class: "mfx-todo-count is-needs", text: String(n) })),
          s.needsYou.map((g) =>
            el(
              "div",
              { class: "mfx-todo-group" },
              el("div", { class: "mfx-todo-group-head" }, g.session ? sessionChip(g.session, known, "group") : el("span", { class: "mfx-todo-muted", text: "No session" })),
              el(
                "div",
                { class: "mfx-todo-rows", role: "list" },
                g.items.map((t) => row(t, "ny:" + g.session, { group: true, known, tierChip: true }))
              )
            )
          )
        )
      );
    }
    if (s.rest.length) {
      parts.push(
        el(
          "section",
          { class: "mfx-todo-section", "aria-label": "To do" },
          s.needsYou.length ? el("h3", { class: "mfx-todo-section-head" }, el("span", { text: "To do" })) : null,
          s.rest.map((g) =>
            el(
              "div",
              { class: "mfx-todo-tier is-" + g.tier },
              s.showTiers ? el("h4", { class: "mfx-todo-tier-head" }, el("span", { text: TIER_LABELS[g.tier] }), el("span", { class: "mfx-todo-tier-n", text: String(g.items.length) })) : null,
              el(
                "div",
                { class: "mfx-todo-rows", role: "list" },
                g.items.map((t) => row(t, "rest", { known, tierChip: !s.showTiers }))
              )
            )
          )
        )
      );
    }
    const done = todos.filter((t) => t.done && !lingering.has(t.id)).length;
    if (done) {
      parts.push(
        el(
          "div",
          { class: "mfx-todo-foot" },
          el("button", { class: "mfx-todo-link-btn", type: "button", "data-key": "foot:history", text: done + " done recently · History", onClick: () => showTab("history") })
        )
      );
    }
    return parts;
  }

  /** The session chip: a button that focuses the session (host level 2), or
   * a muted label when the host cannot, or the session is gone. */
  function sessionChip(session, known, where) {
    const can = typeof (api.ui && api.ui.focusSession) === "function";
    const gone = goneSessions.has(session) || (known && !known.has(session));
    if (!can || gone) {
      return el("span", { class: "mfx-todo-chip is-session is-muted", title: gone ? session + " (no longer open)" : session }, icon("session"), el("span", { class: "mfx-todo-chip-text", text: session }));
    }
    return el(
      "button",
      {
        class: "mfx-todo-chip is-session",
        type: "button",
        title: "Go to " + session,
        "aria-label": "Go to session " + session,
        "data-key": where + ":sess:" + session,
        onClick: (e) => {
          e.stopPropagation();
          let ok = false;
          try {
            ok = api.ui.focusSession(session) !== false;
          } catch (err) {
            api.log.error("focusSession", err);
          }
          if (!ok) {
            goneSessions.add(session);
            showError(session + " is not open any more.");
            draw();
          }
        },
      },
      icon("session"),
      el("span", { class: "mfx-todo-chip-text", text: session })
    );
  }

  function chips(t, opts) {
    const out = [];
    if (t.kind && KIND_LABELS[t.kind]) out.push(el("span", { class: "mfx-todo-chip is-kind is-" + t.kind, text: KIND_LABELS[t.kind] }));
    const tier = tierOf(t);
    if (opts.tierChip && tier !== "normal") out.push(el("span", { class: "mfx-todo-chip is-tier is-" + tier, text: TIER_LABELS[tier] }));
    if (t.session && !opts.group) out.push(sessionChip(t.session, opts.known, "row:" + t.id));
    const src = t.source && t.source !== "you" ? SOURCE_LABELS[t.source] || "from " + t.source : "";
    if (src) out.push(el("span", { class: "mfx-todo-chip is-source", text: src }));
    const due = formatDue(t.due, new Date());
    if (due) {
      out.push(
        el(
          "span",
          { class: "mfx-todo-chip is-due" + (due.overdue ? " is-overdue" : "") + (due.soon ? " is-soon" : ""), title: due.title },
          icon("clock"),
          el("span", { class: "mfx-todo-chip-text", text: due.label })
        )
      );
    }
    if (t.link) {
      out.push(
        el(
          "a",
          { class: "mfx-todo-chip is-link", href: t.link, target: "_blank", rel: "noopener noreferrer", title: t.link, "aria-label": "Open link " + t.link, "data-key": "row:" + t.id + ":link", onClick: (e) => e.stopPropagation() },
          icon("link")
        )
      );
    }
    if (t.details && String(t.details).trim()) {
      out.push(el("span", { class: "mfx-todo-chip is-notes", title: "Has details", "aria-label": "Has details" }, icon("notes")));
    }
    return out;
  }

  /** One open item. `scope` names the run of rows it can be dragged within. */
  function row(t, scope, opts) {
    const isOpen = expanded.has(t.id);
    const editing = editText === t.id;
    const check = el("input", {
      class: "mfx-todo-check",
      type: "checkbox",
      checked: !!t.done,
      "aria-label": "Done: " + t.text,
      "data-key": "chk:" + t.id,
      onChange: (e) => {
        e.stopPropagation();
        setDone(t.id, check.checked);
      },
      onClick: (e) => e.stopPropagation(),
    });
    const grip = el("span", { class: "mfx-todo-grip", title: "Drag to reorder (or Alt+Up / Alt+Down)", "aria-hidden": "true" }, icon("grip"));
    grip.addEventListener("pointerdown", (e) => startDrag(e, t.id, scope, node));
    const text = editing
      ? textEditor(t)
      : el("span", {
          class: "mfx-todo-text",
          text: t.text || "(no text)",
          onDblclick: (e) => {
            e.preventDefault();
            startEditText(t.id);
          },
        });
    const expandBtn = el(
      "button",
      {
        class: "mfx-todo-icon-btn mfx-todo-expand",
        type: "button",
        "aria-expanded": isOpen ? "true" : "false",
        "aria-label": isOpen ? "Hide details" : "Show details",
        title: isOpen ? "Hide details" : "Show details",
        "data-key": "exp:" + t.id,
        onClick: (e) => {
          e.stopPropagation();
          toggleExpand(t.id);
        },
      },
      icon("chevron")
    );
    const node = el(
      "div",
      {
        class: "mfx-todo-row" + (isOpen ? " is-open" : "") + (t.done ? " is-done" : "") + (t.id.startsWith("tmp-") ? " is-pending" : ""),
        role: "listitem",
        tabindex: "0",
        "aria-label": t.text,
        "data-key": "row:" + t.id,
        "data-id": t.id,
        "data-scope": scope,
        onClick: (e) => {
          if (e.detail > 1) return;
          if (e.target.closest("button, a, input, textarea, select, label, .mfx-todo-more, .mfx-todo-grip")) return;
          const sel = window.getSelection && window.getSelection();
          if (sel && !sel.isCollapsed && node.contains(sel.anchorNode)) return;
          toggleExpand(t.id);
        },
      },
      el("div", { class: "mfx-todo-line" }, grip, check, el("div", { class: "mfx-todo-main" }, text, el("span", { class: "mfx-todo-chips" }, chips(t, opts))), expandBtn),
      isOpen ? more(t) : null
    );
    return node;
  }

  function textEditor(t) {
    let cancelled = false;
    let doneEditing = false;
    const input = el("input", {
      class: "mfx-todo-text-edit",
      type: "text",
      value: t.text || "",
      "aria-label": "Edit text",
      "data-key": "edit:" + t.id,
      maxLength: 500,
      onKeydown: (e) => {
        e.stopPropagation();
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          finish(true);
        } else if (e.key === "Escape") {
          e.preventDefault();
          cancelled = true;
          finish(true);
        }
      },
      onBlur: () => finish(false),
      onClick: (e) => e.stopPropagation(),
    });
    function finish(refocus) {
      if (doneEditing) return;
      doneEditing = true;
      editText = null;
      const val = input.value.trim();
      const item = find(t.id);
      const changed = !cancelled && val && item && val !== item.text;
      // Swap back in place (the field may have lost focus to another field
      // in the list, which defers the full redraw).
      const span = el("span", { class: "mfx-todo-text", text: changed ? val : (item && item.text) || "" });
      span.addEventListener("dblclick", (e) => {
        e.preventDefault();
        startEditText(t.id);
      });
      if (input.isConnected) input.replaceWith(span);
      if (refocus) focusKey("row:" + t.id);
      if (changed) {
        void mutate(
          "rename the item",
          () => {
            item.text = val;
          },
          () => call(api, "/api/todo/items/" + encodeURIComponent(t.id), { method: "PATCH", json: { text: val } })
        );
      } else draw();
    }
    return input;
  }

  function focusKey(k) {
    const n = [...list.querySelectorAll("[data-key]")].find((x) => x.getAttribute("data-key") === k);
    if (n) n.focus({ preventScroll: false });
  }

  /** The expanded part of a row: details, editors, delete. */
  function more(t) {
    const fields = el("div", { class: "mfx-todo-fields" });
    const tier = tierOf(t);
    fields.append(
      el("div", { class: "mfx-todo-field" }, el("span", { class: "mfx-todo-field-label", text: "Priority" }), prioritySwitch(tier, (p) => setTier(t.id, p), "pri:" + t.id)),
      dueField(t),
      linkField(t),
      el(
        "button",
        { class: "mfx-todo-btn is-danger mfx-todo-delete", type: "button", "data-key": "del:" + t.id, onClick: (e) => (e.stopPropagation(), removeItem(t.id)) },
        icon("trash"),
        "Delete"
      )
    );
    const meta = [];
    if (t.created_at) {
      const d = new Date(Number(t.created_at) * 1000);
      if (!isNaN(d.getTime())) meta.push("Added " + d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + clock(d));
    }
    if (t.session) meta.push("for " + t.session);
    return el(
      "div",
      { class: "mfx-todo-more", onClick: (e) => e.stopPropagation() },
      detailsArea(t),
      fields,
      meta.length ? el("div", { class: "mfx-todo-meta", text: meta.join(" · ") }) : null
    );
  }

  function detailsArea(t) {
    if (editDetails === t.id) return detailsEditor(t);
    const has = t.details && String(t.details).trim();
    return el(
      "div",
      { class: "mfx-todo-details-wrap" },
      has ? renderDetails(t.details) : null,
      el(
        "button",
        { class: "mfx-todo-btn is-quiet", type: "button", "data-key": "editdet:" + t.id, onClick: () => startEditDetails(t.id) },
        icon("edit"),
        has ? "Edit details" : "Add details"
      )
    );
  }

  function detailsEditor(t) {
    let cancelled = false;
    let doneEditing = false;
    const ta = el("textarea", {
      class: "mfx-todo-details-edit",
      value: t.details || "",
      rows: 3,
      maxLength: 20000,
      placeholder: "Steps, commands (in ``` fences, one per block), links…",
      "aria-label": "Details",
      "data-key": "det:" + t.id,
      onKeydown: (e) => {
        e.stopPropagation();
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          finish(true);
        } else if (e.key === "Escape") {
          e.preventDefault();
          cancelled = true;
          finish(true);
        }
      },
      onBlur: () => finish(false),
    });
    const box = el("div", { class: "mfx-todo-details-wrap is-editing" }, ta, el("div", { class: "mfx-todo-hint", text: "Saved when you click away · Ctrl+Enter saves · Esc cancels" }));
    autoGrow(ta);
    function finish(refocus) {
      if (doneEditing) return;
      doneEditing = true;
      editDetails = null;
      const item = find(t.id);
      const val = ta.value.replace(/\s+$/, "");
      const changed = !cancelled && item && val !== (item.details || "");
      const shown = Object.assign({}, item || t, changed ? { details: val } : {});
      if (box.isConnected) box.replaceWith(detailsArea(shown));
      if (refocus) focusKey("editdet:" + t.id);
      if (changed) {
        void mutate(
          "save the details",
          () => {
            if (val) item.details = val;
            else delete item.details;
          },
          () => call(api, "/api/todo/items/" + encodeURIComponent(t.id), { method: "PATCH", json: { details: val } })
        );
      } else draw();
    }
    return box;
  }

  function dueField(t) {
    const current = parseDue(t.due) ? toDueString(parseDue(t.due)) : "";
    let saveTimer = null;
    const input = el("input", {
      class: "mfx-todo-input mfx-todo-due",
      type: "datetime-local",
      value: current,
      "aria-label": "Due",
      "data-key": "due:" + t.id,
      onChange: () => {
        cancel(saveTimer);
        saveTimer = later(save, 600);
      },
      onBlur: () => save(),
      onKeydown: (e) => {
        e.stopPropagation();
        if (e.key === "Enter") {
          e.preventDefault();
          save();
        }
      },
    });
    function save() {
      cancel(saveTimer);
      saveTimer = null;
      const item = find(t.id);
      if (!item) return;
      const v = input.value || "";
      if (v === (item.due || "")) return;
      setDue(t.id, v);
    }
    const clear = current
      ? el("button", { class: "mfx-todo-icon-btn", type: "button", "aria-label": "Clear the due time", title: "Clear", "data-key": "dueclr:" + t.id, onClick: () => setDue(t.id, "") }, icon("close"))
      : null;
    return el("label", { class: "mfx-todo-field" }, el("span", { class: "mfx-todo-field-label", text: "Due" }), el("span", { class: "mfx-todo-field-ctl" }, input, clear));
  }

  function linkField(t) {
    const input = el("input", {
      class: "mfx-todo-input mfx-todo-linkin",
      type: "url",
      value: t.link || "",
      placeholder: "https://…",
      "aria-label": "Link",
      "data-key": "link:" + t.id,
      onKeydown: (e) => {
        e.stopPropagation();
        if (e.key === "Enter") {
          e.preventDefault();
          input.blur();
        } else if (e.key === "Escape") {
          e.preventDefault();
          input.value = t.link || "";
          input.blur();
        }
      },
      onBlur: () => {
        const item = find(t.id);
        const v = input.value.trim();
        if (!item || v === (item.link || "")) return;
        void mutate(
          "save the link",
          () => {
            if (v) item.link = v;
            else delete item.link;
          },
          () => call(api, "/api/todo/items/" + encodeURIComponent(t.id), { method: "PATCH", json: { link: v } })
        );
      },
    });
    return el("label", { class: "mfx-todo-field is-wide" }, el("span", { class: "mfx-todo-field-label", text: "Link" }), input);
  }

  // --- history ---------------------------------------------------------------

  function drawHistory() {
    if (history === null) {
      if (historyFailed) {
        return [
          el(
            "div",
            { class: "mfx-todo-empty" },
            el("div", { class: "mfx-todo-empty-head", text: "Could not load history" }),
            el("p", { class: "mfx-todo-empty-line", text: historyFailed }),
            el("button", { class: "mfx-todo-btn", type: "button", text: "Try again", onClick: () => loadHistory() })
          ),
        ];
      }
      return [el("p", { class: "mfx-todo-loading", text: "Loading history…" })];
    }
    if (!history.length) {
      return [
        q.trim()
          ? emptyState("Nothing matches", "No finished or removed item mentions “" + q.trim() + "”.")
          : emptyState("No history yet", "Items you check off or delete land here, newest first. Restore brings one back."),
      ];
    }
    const known = knownSessions();
    return groupHistoryByDay(history, new Date()).map((day) =>
      el(
        "section",
        { class: "mfx-todo-day", "aria-label": day.label },
        el("h3", { class: "mfx-todo-section-head" }, el("span", { text: day.label }), el("span", { class: "mfx-todo-tier-n", text: String(day.items.length) })),
        el(
          "div",
          { class: "mfx-todo-rows", role: "list" },
          day.items.map((h) => historyRow(h, known))
        )
      )
    );
  }

  function hKey(h) {
    return h.id + "@" + closedAt(h);
  }

  function historyRow(h, known) {
    const k = hKey(h);
    const isOpen = hExpanded.has(k);
    const removed = h.outcome === "removed";
    const at = closedAt(h);
    const asking = forgetAsk === k;
    const head = el(
      "div",
      { class: "mfx-todo-line" },
      el(
        "span",
        { class: "mfx-todo-outcome is-" + (removed ? "removed" : "done"), title: removed ? "Removed" : "Done" },
        icon(removed ? "trash" : "check"),
        el("span", { class: "mfx-todo-outcome-word", text: removed ? "Removed" : "Done" })
      ),
      el(
        "div",
        { class: "mfx-todo-main" },
        el("span", { class: "mfx-todo-text", text: h.text || "(no text)" }),
        el(
          "span",
          { class: "mfx-todo-chips" },
          h.kind && KIND_LABELS[h.kind] ? el("span", { class: "mfx-todo-chip is-kind is-" + h.kind, text: KIND_LABELS[h.kind] }) : null,
          h.session ? sessionChip(h.session, known, "h:" + k) : null,
          h.details && String(h.details).trim() ? el("span", { class: "mfx-todo-chip is-notes", title: "Has details", "aria-label": "Has details" }, icon("notes")) : null
        )
      ),
      el("span", { class: "mfx-todo-time", text: at ? clock(new Date(at * 1000)) : "" })
    );
    const body = isOpen
      ? el(
          "div",
          { class: "mfx-todo-more", onClick: (e) => e.stopPropagation() },
          h.details && String(h.details).trim() ? renderDetails(h.details) : null,
          el(
            "div",
            { class: "mfx-todo-fields" },
            el("button", { class: "mfx-todo-btn", type: "button", "data-key": "hres:" + k, onClick: () => restore(h) }, icon("restore"), "Restore"),
            asking
              ? el(
                  "span",
                  { class: "mfx-todo-ask", role: "group", "aria-label": "Forget this entry?" },
                  el("span", { text: "Forget for good?" }),
                  el("button", { class: "mfx-todo-btn is-danger", type: "button", text: "Yes", "data-key": "hyes:" + k, onClick: () => forget(h) }),
                  el("button", { class: "mfx-todo-btn", type: "button", text: "No", "data-key": "hno:" + k, onClick: () => ((forgetAsk = null), draw(), focusKey("hfor:" + k)) })
                )
              : el("button", { class: "mfx-todo-btn is-quiet", type: "button", "data-key": "hfor:" + k, onClick: () => ((forgetAsk = k), draw(), focusKey("hno:" + k)) }, icon("trash"), "Forget")
          )
        )
      : null;
    const node = el(
      "div",
      {
        class: "mfx-todo-hrow" + (isOpen ? " is-open" : "") + (removed ? " is-removed" : ""),
        role: "listitem",
        tabindex: "0",
        "aria-expanded": isOpen ? "true" : "false",
        "aria-label": (removed ? "Removed: " : "Done: ") + (h.text || ""),
        "data-key": "hrow:" + k,
        "data-hkey": k,
        onClick: (e) => {
          if (e.target.closest("button, a, input, textarea, .mfx-todo-more")) return;
          toggleHistory(k);
        },
      },
      head,
      body
    );
    return node;
  }

  function toggleHistory(k) {
    if (hExpanded.has(k)) hExpanded.delete(k);
    else hExpanded.add(k);
    if (forgetAsk === k) forgetAsk = null;
    draw();
  }

  async function restore(h) {
    const r = await mutate("restore the item", null, () => call(api, "/api/todo/history/" + encodeURIComponent(h.id) + "/restore", { method: "POST" }));
    if (!r) return;
    hExpanded.delete(hKey(h));
    history = (history || []).filter((x) => hKey(x) !== hKey(h));
    draw();
    showUndo("Restored · " + (h.text || ""), () => showTab("todo"), "Show");
    scheduleHistory(300);
  }

  async function forget(h) {
    const k = hKey(h);
    forgetAsk = null;
    const prev = history;
    const r = await mutate(
      "forget the entry",
      () => {
        history = (history || []).filter((x) => hKey(x) !== k);
        if (!h.outcome || h.outcome === "done") todos = todos.filter((t) => !(t.id === h.id && t.done));
      },
      () => call(api, "/api/todo/history/" + encodeURIComponent(h.id), { method: "DELETE" })
    );
    if (!r) {
      history = prev;
      scheduleHistory(0);
    }
  }

  // --- actions ---------------------------------------------------------------

  function toggleExpand(id) {
    if (expanded.has(id)) {
      expanded.delete(id);
      if (editDetails === id) editDetails = null;
    } else expanded.add(id);
    draw();
  }

  // The editor takes focus in the same task as the redraw: a key typed
  // right after the click must never reach the row (Space = done).
  function startEditText(id) {
    if (!find(id)) return;
    editText = id;
    draw(true);
    const input = list.querySelector(".mfx-todo-text-edit");
    if (input) {
      input.focus();
      input.select();
    }
  }

  function startEditDetails(id) {
    editDetails = id;
    expanded.add(id);
    draw(true);
    const ta = list.querySelector(".mfx-todo-details-edit");
    if (ta) {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }
  }

  async function quickAdd() {
    const text = addInput.value.trim();
    if (!text) return;
    addInput.value = "";
    const body = { text, source: "you" };
    const s = tagOn ? currentSession() : "";
    if (s) body.session = s;
    const tmp = Object.assign({ id: "tmp-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), done: false, created_at: Date.now() / 1000 }, body);
    const r = await mutate(
      "add the item",
      () => {
        todos = sortByTier(openItems(todos).concat([tmp])).concat(todos.filter((t) => t.done));
      },
      () => call(api, "/api/todo/items", { json: body })
    );
    const added = r && Array.isArray(r.added) && r.added[0];
    const updated = r && Array.isArray(r.updated) && r.updated[0];
    const real = added || updated;
    if (real && real.id) todos = todos.map((t) => (t.id === tmp.id ? real : t)).filter((t, i, all) => all.findIndex((x) => x.id === t.id) === i);
    else todos = todos.filter((t) => t.id !== tmp.id);
    if (!r && !addInput.value) addInput.value = text;
    draw();
  }

  function setDone(id, done) {
    const item = find(id);
    if (!item) return;
    // Checked off here: the row stays, crossed out, until the person leaves
    // the list (leaveList) — a slip is one click to undo, and the list does
    // not jump under the pointer.
    if (done) lingering.add(id);
    else lingering.delete(id);
    void mutate(
      done ? "check the item off" : "reopen the item",
      () => {
        item.done = done;
        if (done) item.done_at = Date.now() / 1000;
        else delete item.done_at;
      },
      () => call(api, "/api/todo/items/" + encodeURIComponent(id), { method: "PATCH", json: { done } })
    );
    hideUndo();
  }

  /** The person left the list (another tab, another window, the pane out of
   * sight): the items they checked off go to History's side for good. */
  function leaveList() {
    if (!lingering.size) return;
    lingering.clear();
    draw();
  }

  function removeItem(id) {
    const item = find(id);
    if (!item) return;
    const before = sortByTier(openItems(todos)).map((t) => t.id);
    expanded.delete(id);
    void mutate(
      "delete the item",
      () => {
        todos = todos.filter((t) => t.id !== id);
      },
      () => call(api, "/api/todo/items/" + encodeURIComponent(id), { method: "DELETE" })
    );
    showUndo("Deleted · " + item.text, () =>
      mutate(
        "restore the item",
        () => {
          todos = applyOrder(todos.concat([Object.assign({}, item)]), before);
        },
        async () => {
          await call(api, "/api/todo/history/" + encodeURIComponent(id) + "/restore", { method: "POST" });
          // Restore lands at the end of its tier: put it back where it was.
          return call(api, "/api/todo/order", { json: { ids: before } });
        }
      )
    );
  }

  function setTier(id, tier) {
    const item = find(id);
    if (!item || tierOf(item) === tier) return;
    void mutate(
      "change the priority",
      () => {
        todos = applyTier(todos, id, tier);
      },
      () => call(api, "/api/todo/items/" + encodeURIComponent(id), { method: "PATCH", json: { priority: tier } })
    );
  }

  function setDue(id, due) {
    const item = find(id);
    if (!item) return;
    void mutate(
      "save the due time",
      () => {
        if (due) item.due = due;
        else delete item.due;
      },
      () => call(api, "/api/todo/items/" + encodeURIComponent(id), { method: "PATCH", json: { due } })
    );
  }

  /** Apply a move computed by orderAfterDrop / orderAfterStep. */
  function applyMove(id, move) {
    if (!move || !move.changed) return;
    void mutate(
      "reorder",
      () => {
        if (move.priority) todos = applyTier(todos, id, move.priority);
        todos = applyOrder(todos, move.ids);
      },
      async () => {
        if (move.priority) {
          await call(api, "/api/todo/items/" + encodeURIComponent(id), { method: "PATCH", json: { priority: move.priority } });
        }
        return call(api, "/api/todo/order", { json: { ids: move.ids } });
      }
    );
  }

  function scopeRows(scope) {
    return [...list.querySelectorAll(".mfx-todo-row")].filter((r) => r.dataset.scope === scope);
  }

  function step(id, dir) {
    const node = list.querySelector('.mfx-todo-row[data-id="' + CSS.escape(id) + '"]');
    if (!node) return;
    const moving = find(id);
    if (!moving || moving.done) return;
    const visible = scopeRows(node.dataset.scope)
      .map((r) => find(r.dataset.id))
      .filter((t) => t && !t.done);
    applyMove(id, orderAfterStep(sortByTier(openItems(todos)), visible, id, dir));
  }

  // --- drag ------------------------------------------------------------------

  function startDrag(e, id, scope, node) {
    if (e.button !== 0 || tab !== "todo") return;
    const dragged = find(id);
    if (dragged && dragged.done) return;
    e.preventDefault();
    e.stopPropagation();
    const grip = e.currentTarget;
    try {
      grip.setPointerCapture(e.pointerId);
    } catch (_) {
      /* synthetic */
    }
    drag = { id, scope, node, grip, pointerId: e.pointerId, y0: e.clientY, y: e.clientY, active: false, target: null, raf: 0 };
    grip.addEventListener("pointermove", onDragMove);
    grip.addEventListener("pointerup", onDragEnd);
    grip.addEventListener("pointercancel", onDragCancel);
    window.addEventListener("keydown", onDragKey, true);
  }

  function onDragMove(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    drag.y = e.clientY;
    if (!drag.active) {
      if (Math.abs(drag.y - drag.y0) < 4) return;
      drag.active = true;
      drag.node.classList.add("is-dragging");
      root.classList.add("is-dragging");
      const tick = () => {
        if (!drag || !drag.active) return;
        const r = scroll.getBoundingClientRect();
        const edge = 36;
        if (drag.y < r.top + edge) scroll.scrollTop -= Math.ceil((r.top + edge - drag.y) / 4);
        else if (drag.y > r.bottom - edge) scroll.scrollTop += Math.ceil((drag.y - (r.bottom - edge)) / 4);
        track();
        drag.raf = requestAnimationFrame(tick);
      };
      drag.raf = requestAnimationFrame(tick);
    }
    track();
  }

  /** Where would a drop land now? Sets drag.target and places the line. */
  function track() {
    const rows = scopeRows(drag.scope).filter((r) => r !== drag.node);
    if (!rows.length) {
      drag.target = null;
      dropLine.hidden = true;
      return;
    }
    let anchor = rows.find((r) => {
      const b = r.getBoundingClientRect();
      return drag.y < b.top + b.height / 2;
    });
    let place = "before";
    if (!anchor) {
      anchor = rows[rows.length - 1];
      place = "after";
    }
    drag.target = { anchorId: anchor.dataset.id, place };
    const box = anchor.getBoundingClientRect();
    const base = scroll.getBoundingClientRect();
    const y = (place === "before" ? box.top - 3 : box.bottom + 2) - base.top + scroll.scrollTop;
    dropLine.style.top = Math.round(y) + "px";
    dropLine.style.left = Math.round(box.left - base.left) + "px";
    dropLine.style.width = Math.round(box.width) + "px";
    const from = find(drag.id);
    const to = find(anchor.dataset.id);
    const tierTo = to ? tierOf(to) : null;
    const label = dropLine.firstChild;
    if (from && tierTo && tierOf(from) !== tierTo) {
      label.textContent = "Move to " + TIER_LABELS[tierTo];
      label.hidden = false;
    } else {
      label.textContent = "";
      label.hidden = true;
    }
    dropLine.hidden = false;
  }

  function endDrag(commit) {
    const d = drag;
    if (!d) return;
    drag = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    d.grip.removeEventListener("pointermove", onDragMove);
    d.grip.removeEventListener("pointerup", onDragEnd);
    d.grip.removeEventListener("pointercancel", onDragCancel);
    window.removeEventListener("keydown", onDragKey, true);
    try {
      d.grip.releasePointerCapture(d.pointerId);
    } catch (_) {
      /* already released */
    }
    d.node.classList.remove("is-dragging");
    root.classList.remove("is-dragging");
    dropLine.hidden = true;
    if (commit && d.active && d.target) {
      applyMove(d.id, orderAfterDrop(sortByTier(openItems(todos)), d.id, d.target.anchorId, d.target.place));
    }
    if (pendingDraw) draw();
  }

  function onDragEnd(e) {
    if (drag && e.pointerId === drag.pointerId) endDrag(true);
  }
  function onDragCancel(e) {
    if (drag && e.pointerId === drag.pointerId) endDrag(false);
  }
  function onDragKey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      endDrag(false);
    }
  }

  // --- keyboard --------------------------------------------------------------

  function allRows() {
    return [...list.querySelectorAll(".mfx-todo-row, .mfx-todo-hrow")];
  }

  function onKey(e) {
    if (disposed || e.defaultPrevented) return;
    const t = e.target;
    if (isTextEntry(t)) return;
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    if (plain && (e.key === "n" || e.key === "N") && !e.shiftKey) {
      e.preventDefault();
      if (tab !== "todo") showTab("todo");
      addInput.focus();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "z" || e.key === "Z") && undo) {
      e.preventDefault();
      runUndo();
      return;
    }
    const rowNode = t && t.closest ? t.closest(".mfx-todo-row, .mfx-todo-hrow") : null;
    const rows = allRows();
    if (!rowNode || !list.contains(rowNode)) {
      // From the tabs / filter area: ArrowDown enters the list.
      if (plain && e.key === "ArrowDown" && rows.length && head.contains(t)) {
        e.preventDefault();
        rows[0].focus();
      }
      return;
    }
    const onRow = t === rowNode;
    const isTodo = rowNode.classList.contains("mfx-todo-row");
    const id = rowNode.dataset.id;
    const at = rows.indexOf(rowNode);
    if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      if (!isTodo) return;
      e.preventDefault();
      step(id, e.key === "ArrowUp" ? -1 : 1);
      return;
    }
    if (!plain) return;
    switch (e.key) {
      case "ArrowUp":
      case "ArrowDown": {
        e.preventDefault();
        const next = rows[at + (e.key === "ArrowUp" ? -1 : 1)];
        if (next) next.focus();
        else if (e.key === "ArrowUp" && tab === "todo") addInput.focus();
        return;
      }
      case "Home":
      case "End":
        if (!onRow) return;
        e.preventDefault();
        (e.key === "Home" ? rows[0] : rows[rows.length - 1]).focus();
        return;
      case " ":
        if (!onRow || !isTodo) return;
        e.preventDefault();
        setDone(id, true);
        return;
      case "Enter":
      case "F2":
        if (!onRow) return;
        e.preventDefault();
        if (isTodo) startEditText(id);
        else toggleHistory(rowNode.dataset.hkey);
        return;
      case "ArrowRight":
      case "ArrowLeft": {
        if (!onRow) return;
        e.preventDefault();
        const want = e.key === "ArrowRight";
        if (isTodo) {
          if (expanded.has(id) !== want) toggleExpand(id);
        } else if (hExpanded.has(rowNode.dataset.hkey) !== want) toggleHistory(rowNode.dataset.hkey);
        return;
      }
      case "1":
      case "2":
      case "3":
        if (!isTodo) return;
        e.preventDefault();
        setTier(id, { 1: "high", 2: "normal", 3: "low" }[e.key]);
        return;
      case "Delete":
        if (!onRow || !isTodo) return;
        e.preventDefault();
        removeItem(id);
        return;
      default:
    }
  }

  /** A deferred redraw runs once the person leaves the field. */
  function onFocusOut() {
    if (!pendingDraw) return;
    later(() => {
      if (pendingDraw && !typingInList() && !drag) draw();
    }, 0);
  }

  root.addEventListener("keydown", onKey);
  list.addEventListener("focusout", onFocusOut);
  const onEnter = () => tag.refresh();
  root.addEventListener("pointerenter", onEnter);
  scroll.tabIndex = -1;

  // --- start -----------------------------------------------------------------
  const view = { showTab };
  live.add(view);
  showTab(tab);
  draw(true);
  void load();

  const tick = setInterval(() => {
    void load();
    if (tab === "history") scheduleHistory(0);
  }, REFRESH_MS);
  const focusPoll = setInterval(() => tag.refresh(), FOCUS_POLL_MS);
  if (api.events && typeof api.events.subscribe === "function") {
    try {
      unsubs.push(
        api.events.subscribe("addon.todo.changed", () => {
          scheduleLoad(150);
          if (tab === "history" || history !== null) scheduleHistory(300);
        })
      );
    } catch (err) {
      api.log.error("events", err);
    }
  }

  // Out of sight = left: a pane hidden behind another window, a grid that no
  // longer shows it, the app in the background.
  let seen = null;
  if (typeof IntersectionObserver === "function") {
    seen = new IntersectionObserver((entries) => {
      if (entries.some((en) => !en.isIntersecting)) leaveList();
    });
    seen.observe(root);
  }
  const onHidden = () => {
    if (document.hidden) leaveList();
  };
  document.addEventListener("visibilitychange", onHidden);

  return {
    dispose() {
      disposed = true;
      if (seen) seen.disconnect();
      document.removeEventListener("visibilitychange", onHidden);
      live.delete(view);
      if (drag) endDrag(false);
      clearInterval(tick);
      clearInterval(focusPoll);
      for (const t of timers) clearTimeout(t);
      timers.clear();
      for (const u of unsubs) {
        try {
          if (typeof u === "function") u();
        } catch (_) {
          /* ignore */
        }
      }
      root.removeEventListener("keydown", onKey);
      root.removeEventListener("pointerenter", onEnter);
      list.removeEventListener("focusout", onFocusOut);
      root.classList.remove("mfx-todo-root", "is-dragging");
      root.replaceChildren();
    },
  };
}
