/** The SQL query pane (surface "query", multi-instance).
 *
 * Toolbar: connection + database (+ schema, where the engine has them)
 * selectors, Run (Ctrl/Cmd+Enter — the statement under the caret, or the
 * selection), Run All (sequential, stops on the first error), history (the
 * last 50 statements, in api.storage), and CSV/JSON export of the statement
 * under the caret. Below: a monospace textarea and a read-only results grid
 * with an info line ("n rows · x ms" / "affected n").
 *
 * The typed SQL and the last result live in this pane's keep-alive DOM — the
 * host detaches and re-adopts the same element across grid drags, so nothing
 * here needs to save and restore. The server decides when a write needs
 * confirmation (needs_confirm); this pane only words the bar and resends with
 * confirm: true. */

import { el, button, option, confirmBar, dismissConfirm, makeNotice, showOverlay, errMsg, fmtNum, fmtBytes } from "./ui.js";
import { createGrid } from "./grid.js";
import { splitStatements, statementAt, classifyStatement } from "./sql.js";
import { connUrl, createScopePicker } from "./explorer.js";

const HISTORY_KEY = "history";
const HISTORY_MAX = 50;

/** Content-Disposition → filename (filename* wins, then filename=). */
export function filenameFromDisposition(header, fallback) {
  const h = String(header || "");
  const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(h);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch (e) {
      /* fall through */
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/.exec(h);
  return plain ? plain[1].trim() : fallback;
}

export function renderQueryPad(shared, host) {
  const api = shared.api;
  const ctx = host.ctx && typeof host.ctx === "object" ? host.ctx : {};
  let disposed = false;
  let running = false;
  let dialect = "standard";

  // --- DOM ----------------------------------------------------------------
  const picker = createScopePicker(api, {
    connId: ctx.connId,
    database: ctx.database,
    schema: ctx.schema,
    onChange: (s) => {
      dialect = s.engine === "mysql" ? "mysql" : "standard";
      const title = "SQL" + (s.connName ? " · " + s.connName : "");
      host.setTitle(title);
      shared.setQueryTitle(host.ref, title);
      runBtn.disabled = runAllBtn.disabled = !s.connId;
    },
    onError: (err) => notice.set("Could not load connection scope: " + errMsg(err), "error"),
  });
  const runBtn = button("Run", { kind: "primary", icon: "play", title: "Run the statement at the cursor (Ctrl+Enter)", disabled: true, onClick: () => runAtCursor() });
  const runAllBtn = button("Run all", { icon: "play-all", title: "Run every statement in order, stopping at the first error (Ctrl+Shift+Enter)", disabled: true, onClick: () => runAll() });
  const historySel = el("select", { class: "dbc-select dbc-history", title: "Recent statements" });
  historySel.addEventListener("change", () => {
    const v = historySel.value;
    if (v !== "") {
      const hist = history();
      const sql = hist[Number(v)];
      if (sql !== undefined) {
        textarea.value = sql;
        textarea.focus();
      }
    }
    historySel.value = "";
  });
  const toolbar = el(
    "div",
    { class: "dbc-toolbar" },
    picker.el,
    runBtn,
    runAllBtn,
    el("span", { class: "dbc-spacer" }),
    historySel,
    button("CSV", { icon: "download", title: "Export the statement at the cursor as CSV (up to 10,000 rows)", onClick: () => exportAtCursor("csv") }),
    button("JSON", { icon: "download", title: "Export the statement at the cursor as JSON (up to 10,000 rows)", onClick: () => exportAtCursor("json") })
  );
  const textarea = el("textarea", {
    class: "dbc-sql mono",
    spellcheck: false,
    placeholder: "SELECT …   (Ctrl+Enter runs the statement at the cursor)",
    value: typeof ctx.sql === "string" ? ctx.sql : "",
    "aria-label": "SQL",
  });
  textarea.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (e.shiftKey) runAll();
      else runAtCursor();
    }
  });
  const info = el("div", { class: "dbc-info muted" });
  const notice = makeNotice();
  const confirmSlot = el("div", { class: "dbc-confirm-slot" });
  const grid = createGrid({
    onInspect: (v) =>
      showOverlay(root, {
        title: "Cell value (read only)",
        note: "first " + fmtBytes(String(v.text || "").length) + " of " + fmtBytes(v.len),
        text: String(v.text || ""),
      }),
  });
  const results = el("div", { class: "dbc-results" }, info, notice.el, confirmSlot, grid.el);
  const root = el("div", { class: "dbc-pane dbc-querypad" }, toolbar, textarea, results);
  host.el.appendChild(root);
  renderHistory();
  grid.setData({ columns: [], rows: [] });
  info.textContent = "Ready.";

  // --- history ------------------------------------------------------------
  function history() {
    const h = api.storage.get(HISTORY_KEY, []);
    return Array.isArray(h) ? h.filter((x) => typeof x === "string") : [];
  }
  function remember(sql) {
    const trimmed = sql.trim();
    if (!trimmed) return;
    const next = [trimmed, ...history().filter((x) => x !== trimmed)].slice(0, HISTORY_MAX);
    api.storage.set(HISTORY_KEY, next);
    renderHistory();
  }
  function renderHistory() {
    historySel.replaceChildren(option("", "History…"));
    history().forEach((sql, i) => {
      const line = sql.replace(/\s+/g, " ").trim();
      historySel.appendChild(option(String(i), line.length > 70 ? line.slice(0, 67) + "…" : line));
    });
    historySel.value = "";
  }

  // --- running ------------------------------------------------------------
  function currentStatement() {
    const { selectionStart, selectionEnd, value } = textarea;
    if (selectionStart !== selectionEnd) {
      const text = value.slice(selectionStart, selectionEnd).trim();
      return text ? { text, start: selectionStart, end: selectionEnd } : null;
    }
    return statementAt(value, selectionStart, { dialect });
  }

  function setBusy(on) {
    running = on;
    runBtn.disabled = on || !picker.scope().connId;
    runAllBtn.disabled = runBtn.disabled;
    root.classList.toggle("busy", on);
  }

  async function runAtCursor() {
    if (running) return;
    const stmt = currentStatement();
    if (!stmt) {
      info.textContent = "Nothing to run — put the cursor on a statement.";
      return;
    }
    setBusy(true);
    try {
      await runOne(stmt.text, false);
    } finally {
      if (!disposed) setBusy(false);
    }
  }

  async function runAll() {
    if (running) return;
    const stmts = splitStatements(textarea.value, { dialect });
    if (!stmts.length) {
      info.textContent = "Nothing to run.";
      return;
    }
    setBusy(true);
    try {
      for (let i = 0; i < stmts.length; i += 1) {
        const r = await runOne(stmts[i].text, false, i + 1 + "/" + stmts.length);
        if (disposed) return;
        if (!r.ok) {
          if (!r.cancelled) info.textContent = "Stopped at statement " + (i + 1) + " of " + stmts.length + " — " + info.textContent;
          return;
        }
      }
      info.textContent = stmts.length + " statements ran · " + info.textContent;
    } finally {
      if (!disposed) setBusy(false);
    }
  }

  /** Run one statement; on needs_confirm show the bar and resend with
   * confirm: true. Returns {ok, cancelled}. */
  async function runOne(sql, confirm, progress) {
    const s = picker.scope();
    if (!s.connId) {
      notice.set("Choose a connection first.", "error");
      return { ok: false };
    }
    notice.clear();
    info.textContent = (progress ? "[" + progress + "] " : "") + "Running…";
    const body = { sql };
    if (s.database) body.database = s.database;
    if (s.schema) body.schema = s.schema;
    if (confirm) body.confirm = true;
    let res;
    try {
      res = await api.request(connUrl(s.connId, "/query"), { json: body });
    } catch (err) {
      if (disposed) return { ok: false };
      notice.set(errMsg(err), "error");
      info.textContent = "Request failed.";
      return { ok: false };
    }
    if (disposed) return { ok: false };
    if (!res || typeof res !== "object") {
      notice.set("Unexpected reply from the server.", "error");
      return { ok: false };
    }
    if (res.needs_confirm) {
      const cls = classifyStatement(sql, { dialect });
      const what = cls.verb || "This statement";
      const yes = await confirmBar(confirmSlot, {
        danger: true,
        title: what + (cls.hasWhere ? " needs confirmation" : " has no WHERE clause — it will affect every row"),
        pre: sql,
        confirmLabel: "Run anyway",
      });
      if (disposed) return { ok: false };
      if (!yes) {
        info.textContent = "Cancelled.";
        return { ok: false, cancelled: true };
      }
      return runOne(sql, true, progress);
    }
    remember(sql);
    if (res.ok === false) {
      notice.set(res.error || "The statement failed.", "error");
      info.textContent = "Error" + (typeof res.elapsed_ms === "number" ? " · " + fmtNum(Math.round(res.elapsed_ms)) + " ms" : "");
      return { ok: false };
    }
    const cols = Array.isArray(res.columns) ? res.columns : [];
    const rows = Array.isArray(res.rows) ? res.rows : [];
    const ms = typeof res.elapsed_ms === "number" ? fmtNum(Math.round(res.elapsed_ms)) + " ms" : "";
    if (cols.length) {
      grid.setData({ columns: cols, rows });
      const n = typeof res.row_count === "number" ? res.row_count : rows.length;
      let line = fmtNum(n) + (n === 1 ? " row" : " rows") + (ms ? " · " + ms : "");
      if (res.truncated) line += " · truncated at " + fmtNum(n) + " rows (raise max_rows or export)";
      info.textContent = line;
    } else {
      grid.setData({ columns: [], rows: [] });
      const affected = typeof res.affected === "number" ? res.affected : null;
      info.textContent = (affected === null ? "OK" : "affected " + fmtNum(affected)) + (ms ? " · " + ms : "");
    }
    return { ok: true };
  }

  // --- export -------------------------------------------------------------
  async function exportAtCursor(format) {
    const stmt = currentStatement();
    const s = picker.scope();
    if (!stmt || !s.connId) {
      info.textContent = "Put the cursor on a statement to export its rows.";
      return;
    }
    notice.clear();
    info.textContent = "Exporting…";
    const body = { sql: stmt.text, format };
    if (s.database) body.database = s.database;
    if (s.schema) body.schema = s.schema;
    try {
      // Plain fetch, not api.request: the body is a file (raw bytes + the
      // Content-Disposition filename), not JSON to parse. Same-origin cookies
      // ride along exactly as they do for the wrapper.
      const r = await fetch(connUrl(s.connId, "/export"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (disposed) return;
      const type = r.headers.get("content-type") || "";
      if (!r.ok || (type.includes("application/json") && format !== "json")) {
        let msg = "export failed (" + r.status + ")";
        try {
          const j = await r.json();
          if (j && (j.error || j.detail)) msg = j.error || j.detail;
        } catch (e) {
          /* not JSON */
        }
        notice.set(msg, "error");
        info.textContent = "Export failed.";
        return;
      }
      const blob = await r.blob();
      if (format === "json" && type.includes("application/json")) {
        // A JSON error body and a JSON export share a content type; sniff for
        // the chokepoint's {ok:false} shape before offering it as a download.
        const head = await blob.slice(0, 256).text();
        if (/^\s*\{\s*"ok"\s*:\s*false/.test(head)) {
          let msg = "export failed";
          try {
            msg = JSON.parse(await blob.text()).error || msg;
          } catch (e) {
            /* keep generic */
          }
          notice.set(msg, "error");
          info.textContent = "Export failed.";
          return;
        }
      }
      const name = filenameFromDisposition(r.headers.get("content-disposition"), "query." + format);
      const url = URL.createObjectURL(blob);
      const a = el("a", { href: url, download: name, hidden: true });
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      info.textContent = "Exported " + name + " (" + fmtBytes(blob.size) + ").";
    } catch (err) {
      if (disposed) return;
      notice.set(errMsg(err), "error");
      info.textContent = "Export failed.";
    }
  }

  return {
    dispose() {
      disposed = true;
      dismissConfirm(confirmSlot);
      picker.dispose();
      grid.destroy();
      root.remove();
      shared.releaseQueryRef(host.ref);
    },
  };
}
