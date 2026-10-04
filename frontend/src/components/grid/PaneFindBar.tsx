/** In-place find bar (Ctrl+F on a live terminal): the pane itself scrolls to
 * each hit, like a browser's find — never a separate history view.
 *
 * It searches whichever scrollback the pane's mouse wheel moves, which the
 * server decides per pane (GET …/find → backend pane_find.find_mode) so it
 * holds for every agent CLI:
 *
 * - "tmux": the app leaves the mouse to tmux (shells, codex, aider, gemini…),
 *   so it's tmux copy-mode search. tmux scrolls the pane and paints the hits
 *   itself; the bar shows "3 / 10".
 * - "scroll": the app scrolls itself (Claude Code, opencode, cline…). The
 *   moment the bar opens the server indexes everything the app would show
 *   (backend pane_scroll_find: a briefly tall window, read in a sweep), so
 *   the count is there before anything moves and each step is one jump. The
 *   view is FROZEN (a snapshot of the terminal laid over it) while a step
 *   runs, so none of that is ever seen — only the landing. The hits are
 *   painted HERE, over the terminal (FindHighlights), since nothing else
 *   paints them.
 *
 * You start at the bottom, so Enter walks UP to older hits (tmux's own `n`
 * after a backward search); Shift+Enter walks back down. */

import { useCallback, useEffect, useRef, useState } from "react";
import { instApi } from "../../api/client";
import { freezeTerm, peekTerm } from "../../lib/terminals";
import { FindHighlights } from "./FindHighlights";
import { buildPattern, type FindOptions } from "../../lib/screenFind";

type Op = "prepare" | "search" | "older" | "newer" | "close";
export type FindMode = "tmux" | "scroll";

interface FindResult {
  mode?: FindMode;
  total?: number;
  index?: number;
  status?: "found" | "none" | "ready" | "cancelled" | "error" | "closed";
  row?: number;
  col?: number;
  len?: number;
  /** The current hit's spans (row, code-point col, length): the hit and
   * its proximity partner when on screen. */
  spans?: Array<[number, number, number]>;
  region?: [number, number] | null;
  error?: string;
}

export function PaneFindBar({
  title,
  pane,
  mode,
  initialQuery,
  onClose,
}: {
  title: string;
  pane: "agent" | "shell";
  mode: FindMode;
  initialQuery: string;
  /** The bar is gone; the server puts the pane back on the live screen. */
  onClose: () => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  // Options, as in an editor's find bar (backend core/find_query).
  const [opts, setOpts] = useState<FindOptions>({ case: false, word: false, regex: false });
  const [nearOpen, setNearOpen] = useState(false);
  const [near, setNear] = useState("");
  const [within, setWithin] = useState(0);
  const [res, setRes] = useState<FindResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  // Steps are stateful (each moves the view), so they land in the order they
  // were asked for — one chain, never in parallel. Only a cancel jumps it.
  const chain = useRef<Promise<void>>(Promise.resolve());
  const inflight = useRef(0);
  const [frozen, setFrozen] = useState(false);
  const thaw = useRef<(() => void) | null>(null);

  // Everything that decides what a hit is, as one comparable key.
  const spec = {
    query,
    ...opts,
    near: nearOpen ? near : "",
    within: nearOpen ? within : 0,
  };
  const specKey = JSON.stringify(spec);
  const specRef = useRef(spec);
  specRef.current = spec;
  const keyRef = useRef(specKey);
  keyRef.current = specKey;

  // Checked here too, so a half-typed regex says so at once.
  const patterns: RegExp[] = [];
  let localError = "";
  for (const t of [query, nearOpen ? near : ""]) {
    if (!t) continue;
    const pat = buildPattern(t, opts);
    if (pat instanceof Error) localError = pat.message;
    else patterns.push(pat);
  }

  const post = useCallback(
    (op: Op | "cancel", body: object) =>
      instApi<FindResult>(title, "/find", { json: { pane, op, ...body } }),
    [title, pane]
  );

  const run = useCallback(
    (op: Op) => {
      const body = op === "close" || op === "prepare" ? {} : specRef.current;
      const key = keyRef.current;
      // A close must not queue behind a long index it makes pointless.
      if (inflight.current && op === "close") post("cancel", {}).catch(() => {});
      inflight.current += 1;
      setBusy(true);
      // Scroll mode moves the app's view around (a tall window, a sweep, the
      // jump): show a still snapshot until it's done. Not for the close —
      // returning to live is the one move the reader asked to see.
      if (mode === "scroll" && op !== "close" && !thaw.current) {
        thaw.current = freezeTerm(title, pane);
        setFrozen(true);
      }
      chain.current = chain.current.then(async () => {
        try {
          const r = await post(op, body);
          // A step for a query the reader has since changed is stale news.
          if (op !== "close" && op !== "prepare" && key === keyRef.current && r.status !== "cancelled") {
            setRes(r);
            setError(r.status === "error" ? r.error || "find failed" : "");
          } else if (op === "prepare" && r.status === "error") {
            setError(r.error || "find failed");
          }
        } catch (e) {
          if (op !== "close") setError((e as Error).message || "find failed");
        } finally {
          inflight.current -= 1;
          if (!inflight.current) {
            setBusy(false);
            const done = thaw.current;
            thaw.current = null;
            if (done) {
              done();
              setFrozen(false);
            }
          }
        }
      });
      return chain.current;
    },
    [post, mode, title, pane]
  );

  // Scroll mode: index now, while the reader types — the count is then
  // instant, and a find reopened on an unchanged pane reuses it.
  useEffect(() => {
    if (mode === "scroll") run("prepare");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Search as you type (debounced) — and on any option change. Every new
  // search starts from the view.
  useEffect(() => {
    if (!query || localError || (nearOpen && !near)) {
      setRes(null);
      setError(localError);
      if (mode === "tmux" && !query) run("close");
      return;
    }
    setError("");
    const t = setTimeout(() => run("search"), 140);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specKey, localError, run, mode]);

  // Leaving (Esc, tab switch, pane closed): put the pane back on live, and
  // never leave a frozen snapshot behind.
  useEffect(
    () => () => {
      thaw.current?.();
      thaw.current = null;
      run("close");
    },
    [run]
  );

  const found = !!res?.total;
  const step = (dir: "older" | "newer") => {
    if (found) run(dir);
  };
  const stepRef = useRef(step);
  stepRef.current = step;
  const toggle = (k: keyof FindOptions) => setOpts((o) => ({ ...o, [k]: !o[k] }));
  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;

  // Keys that must work wherever focus is in THIS pane (the terminal too):
  // Esc closes, Ctrl+F refocuses the box, F3 / Ctrl+G step, Alt+C / Alt+W /
  // Alt+R flip match case / whole word / regex (as in VS Code). Capture
  // phase so xterm never sees them.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const a = document.activeElement;
      const paneEl = rootRef.current?.closest(".pane");
      if (!paneEl || !a || !paneEl.contains(a)) return;
      const mod = (e.ctrlKey || e.metaKey) && !e.altKey;
      const alt = e.altKey && !e.ctrlKey && !e.metaKey;
      const opt = alt ? ({ c: "case", w: "word", r: "regex" } as const)[e.key.toLowerCase() as "c"] : undefined;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
      } else if (mod && (e.key === "f" || e.key === "F")) {
        e.preventDefault();
        e.stopPropagation();
        inputRef.current?.focus();
        inputRef.current?.select();
      } else if (e.key === "F3" || (mod && (e.key === "g" || e.key === "G"))) {
        e.preventDefault();
        e.stopPropagation();
        stepRef.current(e.shiftKey ? "newer" : "older");
      } else if (opt) {
        e.preventDefault();
        e.stopPropagation();
        toggleRef.current(opt);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  let count = "";
  if (error) count = error;
  else if (!query) count = "";
  else if (nearOpen && !near) count = "…and what?";
  else if (res == null) count = busy && mode === "scroll" ? "Indexing…" : "…";
  else count = res.total ? `${res.index || "?"} / ${res.total}` : "No results";

  const term = peekTerm(title, pane)?.term;
  const current: Array<[number, number, number]> =
    res && res.total && res.row != null
      ? (res.spans ?? [[res.row, res.col ?? 0, res.len ?? 0]])
      : [];
  const optBtn = (k: keyof FindOptions, label: string, tip: string) => (
    <button
      type="button"
      className={"pane-find-opt" + (opts[k] ? " on" : "")}
      title={tip}
      aria-label={tip}
      aria-pressed={opts[k]}
      onClick={() => toggle(k)}
    >
      {label}
    </button>
  );

  return (
    <>
      {term && query && !frozen && patterns.length > 0 && (
        <FindHighlights term={term} patterns={patterns} region={res?.region ?? null} current={current} />
      )}
      <div className="pane-find" role="search" ref={rootRef}>
        <div className="pane-find-row">
          <div className="pane-find-field">
            <input
              ref={inputRef}
              type="text"
              autoFocus
              autoComplete="off"
              spellCheck={false}
              placeholder="Find in pane"
              aria-label="Find in pane"
              value={query}
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  step(e.shiftKey ? "newer" : "older");
                }
              }}
            />
            {optBtn("case", "Aa", "Match case (Alt+C)")}
            {optBtn("word", "ab", "Match whole word (Alt+W)")}
            {optBtn("regex", ".*", "Use regular expression (Alt+R)")}
          </div>
          <span
            className={"pane-find-count" + (error ? " error" : "") + (busy ? " busy" : "")}
            aria-live="polite"
            title={count}
          >
            {count}
          </span>
          <button
            type="button"
            className={"pane-find-btn" + (nearOpen ? " on" : "")}
            title="Find two terms near each other"
            aria-label="Near"
            aria-pressed={nearOpen}
            onClick={() => setNearOpen((v) => !v)}
          >
            near
          </button>
          <button
            type="button"
            className="pane-find-btn"
            title="Older match (Enter)"
            aria-label="Older match"
            disabled={!found}
            onClick={() => step("older")}
          >
            ↑
          </button>
          <button
            type="button"
            className="pane-find-btn"
            title="Newer match (Shift+Enter)"
            aria-label="Newer match"
            disabled={!found}
            onClick={() => step("newer")}
          >
            ↓
          </button>
          <button type="button" className="pane-find-btn" title="Close (Esc)" aria-label="Close find" onClick={onClose}>
            ×
          </button>
        </div>
        {nearOpen ? (
          <div className="pane-find-row pane-find-near">
            <span>…and</span>
            <input
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="second term"
              aria-label="Second term"
              value={near}
              autoFocus
              onChange={(e) => setNear(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  step(e.shiftKey ? "newer" : "older");
                }
              }}
            />
            <span>within</span>
            <select
              aria-label="Lines apart"
              value={within}
              onChange={(e) => setWithin(Number(e.target.value))}
            >
              <option value={0}>same line</option>
              {[1, 2, 3, 5, 10, 20, 50].map((n) => (
                <option key={n} value={n}>
                  {n} line{n > 1 ? "s" : ""}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>
    </>
  );
}
