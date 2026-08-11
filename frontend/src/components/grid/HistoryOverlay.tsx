/** Full-history overlay — browse a pane's whole tmux scrollback like a page.
 *
 * The live terminals mirror tmux: scrolled-back content is repainted in place
 * (tmux copy-mode), so an xterm drag-selection can never extend past the top
 * of the screen — the highlight is anchored to screen cells that tmux
 * rewrites underneath it. This overlay is the answer: it fetches the pane's
 * complete history (GET /api/instances/{title}/history, which falls back to
 * the provider transcript for alt-screen TUIs) and renders it as native DOM
 * text, so scrolling, drag-selecting past the viewport edge, and copying all
 * behave exactly like a web page.
 *
 * Opened from the pane header's history button or by drag-selecting past the
 * top edge of a live terminal (see attachDragHistoryGesture in
 * lib/terminals). Esc or the × closes it. Copy mirrors the terminals' house
 * rule: release the drag and the selection is already on the clipboard. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { copyText } from "../../lib/clipboard";
import type { DragScreenCtx } from "../../lib/terminals";
import { toast } from "../../lib/toast";

/** Caret under a viewport point, across the two DOM APIs. */
function caretAt(x: number, y: number): { node: Node; offset: number } | null {
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  };
  if (doc.caretRangeFromPoint) {
    const r = doc.caretRangeFromPoint(x, y);
    return r ? { node: r.startContainer, offset: r.startOffset } : null;
  }
  if (doc.caretPositionFromPoint) {
    const p = doc.caretPositionFromPoint(x, y);
    return p ? { node: p.offsetNode, offset: p.offset } : null;
  }
  return null;
}

function occurrences(text: string, needle: string): number[] {
  const out: number[] = [];
  let i = text.indexOf(needle);
  while (i >= 0 && out.length < 50) {
    out.push(i);
    i = text.indexOf(needle, i + 1);
  }
  return out;
}

/** Locate the terminal's in-progress selection inside the captured history,
 * returning the offset of the end the drag started from (bottom end for a
 * top-edge drag, top end for a bottom-edge drag).
 *
 * The screen selection rarely matches the capture verbatim: xterm pads each
 * row to the terminal width, the capture joins wrapped rows into one long
 * line (capture-pane -J), and a drag past the bottom edge sweeps up the tmux
 * status line, which is not pane content at all. So after trying the exact
 * string, anchor on the first (bottom edge) / last (top edge) trimmed
 * selection line that exists in the capture — a trimmed visual row is always
 * a contiguous substring of its (possibly joined) captured line — using the
 * neighbouring line to pick between duplicate hits. Falls back to the tail
 * when nothing matches (alt-screen TUIs serve the provider transcript, whose
 * text differs from the screen). */
function findAnchor(
  text: string,
  sel: string,
  edge: "top" | "bottom",
  ctx: DragScreenCtx | null,
  // The TUI's pinned-prompt row at gesture time ("❯ …"): the prompt that
  // governs the visible section. The last-resort anchor — tool output on a
  // TUI screen never reaches the transcript, but its prompt does.
  ghost: string | null
): { anchor: number; matched: boolean; frac?: number } {
  // Best case: locate the whole visible screen block in the capture. It has
  // far more signal than the selection alone, and knowing which captured
  // line was which screen row lets the overlay open with the content parked
  // exactly where the reader left it (same viewport fraction).
  if (ctx) {
    const tLines = text.split("\n");
    const starts: number[] = [];
    let off = 0;
    for (const l of tLines) {
      starts.push(off);
      off += l.length + 1;
    }
    const usable: Array<[number, string]> = [];
    ctx.screen.forEach((s, r) => {
      const t = s.trim();
      // Require real words, not just length: TUI chrome rows (╭───╮ borders,
      // ─── separators) are long but can never match the capture/transcript,
      // and they were crowding real text out of the candidate slots.
      if (t.length >= 4 && (t.match(/[A-Za-z0-9]/g) || []).length >= 4)
        usable.push([r, t]);
    });
    if (usable.length >= 2) {
      // Base candidates: longest lines first (most distinctive), but try
      // several — the longest screen line is often the tmux status bar,
      // which is not pane content and has no hits at all.
      const candidates = [...usable]
        .sort((a, b) => b[1].length - a[1].length)
        .slice(0, 8);
      // The capture's last screen-worth of lines is the CURRENT frame. A
      // scrolled-back TUI redraws OLD content there, so pure recency would
      // anchor on that tail copy — a dead end for downward drags, with the
      // TUI's bottom chrome right below it. On score ties, prefer a match
      // that lies BEFORE the live frame; the tail copy only wins when it is
      // the sole occurrence (a live, unscrolled screen).
      const liveTailStart = Math.max(0, tLines.length - ctx.rows - 2);
      let bestJ = -1,
        bestScore = -1,
        bestBase = 0;
      for (const base of candidates) {
        // Scan from the end (latest first) — re-run commands leave identical
        // earlier copies in the history, and recency breaks those ties.
        const hits: number[] = [];
        for (let j = tLines.length - 1; j >= 0 && hits.length < 50; j--)
          if (tLines[j].includes(base[1])) hits.push(j);
        for (const j of hits) {
          let score = 0;
          for (const [r, s] of usable) {
            if (r === base[0]) continue;
            // ±3 lines of slack: capture-pane -J collapses wrapped rows (and
            // a TUI's transcript fallback wraps/paces lines differently), so
            // screen-row deltas only approximate captured-line deltas.
            const at = j + (r - base[0]);
            for (let d = -3; d <= 3; d++) {
              const tl = tLines[at + d];
              if (tl && tl.includes(s)) {
                score++;
                break;
              }
            }
          }
          const beats =
            score > bestScore ||
            (score === bestScore &&
              bestJ >= liveTailStart &&
              j < liveTailStart);
          if (beats) {
            bestScore = score;
            bestJ = j;
            bestBase = base[0];
          }
        }
      }
      if (bestJ >= 0 && bestScore >= Math.min(2, usable.length - 1)) {
        let line = Math.max(
          0,
          Math.min(tLines.length - 1, bestJ + (ctx.row - bestBase))
        );
        // Row deltas drift by one per wrapped screen line between the base
        // and the anchor (the capture joins wraps) — snap to the anchor
        // row's own content nearby when it's distinctive enough.
        const rowText = (ctx.screen[ctx.row] || "").trim();
        if (rowText.length >= 4 && (rowText.match(/[A-Za-z0-9]/g) || []).length >= 4) {
          for (const d of [0, -1, 1, -2, 2, -3, 3]) {
            const tl = tLines[line + d];
            if (tl && tl.includes(rowText)) {
              line += d;
              break;
            }
          }
        }
        return {
          anchor: starts[line] + Math.min(ctx.col, tLines[line].length),
          matched: true,
          frac: ctx.rows > 0 ? ctx.row / ctx.rows : undefined,
        };
      }
    }
  }
  if (sel) {
    const exact = text.lastIndexOf(sel);
    if (exact >= 0)
      return { anchor: edge === "bottom" ? exact : exact + sel.length, matched: true };
    const lines = sel
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l.length >= 4);
    if (edge === "top") lines.reverse();
    for (let i = 0; i < lines.length; i++) {
      const hits = occurrences(text, lines[i]);
      if (!hits.length) continue;
      let hit = hits[hits.length - 1];
      const buddy = lines[i + 1];
      if (hits.length > 1 && buddy) {
        for (const h of hits) {
          const near =
            edge === "bottom"
              ? text.slice(h, h + lines[i].length + buddy.length + 400)
              : text.slice(Math.max(0, h - buddy.length - 400), h + lines[i].length);
          if (near.includes(buddy)) {
            hit = h;
            break;
          }
        }
      }
      return {
        anchor: edge === "bottom" ? hit : hit + lines[i].length,
        matched: true,
      };
    }
  }
  // Section fallback via the pinned prompt: anchor just past its end — the
  // beginning of the section the reader was inside. Whitespace-stripped
  // matching, since the row is width-wrapped and the transcript is not.
  if (ghost) {
    const want = ghost.replace(/[…]+\s*$/, "").replace(/\s+/g, "");
    if (want.length >= 12) {
      const map: number[] = [];
      let stripped = "";
      for (let i = 0; i < text.length; i++) {
        const c = text.charCodeAt(i);
        if (c !== 32 && c !== 10 && c !== 13 && c !== 9) {
          stripped += text[i];
          map.push(i);
        }
      }
      const j = stripped.lastIndexOf(want);
      if (j >= 0) {
        return {
          anchor: map[Math.min(j + want.length - 1, map.length - 1)] + 1,
          matched: true,
          frac: 0.15,
        };
      }
    }
  }
  return { anchor: text.length, matched: false };
}

// Last-fetched history per pane, so a reopen renders instantly and the fresh
// capture replaces it when it lands (stale-while-revalidate, like DiffTab).
const histCache = new Map<string, string>();
const HIST_CACHE_MAX = 8;

export function HistoryOverlay({
  title,
  pane,
  dragSelection,
  dragEdge,
  dragCtx,
  dragGhost,
  initialPos,
  onClose,
}: {
  title: string;
  pane: "agent" | "shell";
  /** Non-null when opened by the drag-past-the-edge gesture, holding whatever
   * the terminal had selected when the overlay took over: the same drag
   * continues here — selection extends and the view auto-scrolls while the
   * button stays down. Null when opened from the header button or wheel. */
  dragSelection: string | null;
  /** Which terminal edge the gesture crossed: "top" extends the selection
   * upward from its end, "bottom" extends downward from its start. */
  dragEdge: "top" | "bottom";
  /** Visible-screen snapshot at gesture time (see DragScreenCtx) — lets the
   * overlay open with the content parked where the reader left it. */
  dragCtx: DragScreenCtx | null;
  /** The TUI's pinned-prompt row at gesture time — anchor of last resort
   * when the visible rows (tool output) aren't in the transcript. */
  dragGhost: string | null;
  /** Where the view opens: the tail by default, the very top for Ctrl+↑. */
  initialPos: "top" | "bottom";
  onClose: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Scroll intent for the NEXT text commit: "bottom" follows the tail, "top"
  // starts at the beginning (Ctrl+↑), {fromTop} keeps the reader's absolute
  // position (a refresh landing while they browse — new output appends at
  // the END, so distance-from-top is what holds their text still). Applied
  // in the layout effect — scrollHeight is only correct after React commits
  // the new text.
  const pendingScroll = useRef<"top" | "bottom" | { fromTop: number } | null>(
    initialPos
  );

  const load = useCallback(async () => {
    const key = title + " " + pane;
    let t: string;
    try {
      const r = await fetch(
        `/api/instances/${encodeURIComponent(title)}/history?pane=${pane}`
      );
      if (!r.ok) throw new Error((await r.text()) || "HTTP " + r.status);
      t = await r.text();
    } catch (e) {
      setError((e as Error).message || "history fetch failed");
      return;
    }
    setError("");
    histCache.delete(key);
    histCache.set(key, t);
    while (histCache.size > HIST_CACHE_MAX)
      histCache.delete(histCache.keys().next().value!);
    // Fresh capture replacing what's shown: follow the tail if the reader is
    // there, otherwise hold their text still (fromTop). A still-pending
    // initial intent ("top"/"bottom" not yet applied) wins.
    const el = scrollRef.current;
    if (pendingScroll.current == null) {
      const atBottom =
        !el || el.scrollHeight - el.scrollTop - el.clientHeight < 8;
      pendingScroll.current = atBottom ? "bottom" : { fromTop: el!.scrollTop };
    }
    setText(t);
  }, [title, pane]);

  useEffect(() => {
    // Cached history renders instantly; the fetch below revalidates it.
    pendingScroll.current = initialPos;
    setText(histCache.get(title + " " + pane) ?? null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, title, pane]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    const want = pendingScroll.current;
    if (!el || want == null || text == null) return;
    pendingScroll.current = null;
    el.scrollTop =
      want === "top" ? 0 : want === "bottom" ? el.scrollHeight : want.fromTop;
  }, [text]);

  // Live tail: re-capture every few seconds while open, so the page never
  // goes stale — scrolling to the bottom always reaches the present. Skipped
  // while the reader is mid-press or has text selected (a refresh would
  // shift the text under their highlight).
  useEffect(() => {
    const id = setInterval(() => {
      if (pressAt.current) return;
      const sel = window.getSelection?.();
      if (sel && !sel.isCollapsed && sel.toString().trim()) return;
      load();
    }, 3000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  // --- Mid-drag continuation --------------------------------------------
  // Opened by the gesture, the button may still be down. Track it from mount
  // (history takes a beat to fetch; a released button must not start a
  // phantom drag), and suppress the scroller's own copy-on-release when the
  // continuation's finish handler already copied.
  const buttonDown = useRef(dragSelection != null);
  const suppressElementCopy = useRef(false);
  useEffect(() => {
    if (dragSelection == null) return;
    const up = () => {
      buttonDown.current = false;
    };
    window.addEventListener("mouseup", up, true);
    return () => window.removeEventListener("mouseup", up, true);
  }, [dragSelection]);

  const contStarted = useRef(false);
  useEffect(() => {
    if (text == null || dragSelection == null || contStarted.current) return;
    contStarted.current = true;
    const el = scrollRef.current;
    const node = el?.querySelector("pre")?.firstChild;
    if (!el || !node || node.nodeType !== Node.TEXT_NODE) return;
    // Anchor where the terminal drag started: for a top-edge drag that is
    // the bottom end of what was already selected, for a bottom-edge drag
    // the top end. Falls back to the tail when nothing matched (alt-screen
    // TUIs serve the provider transcript, whose text differs from the
    // screen).
    const { anchor, matched, frac } = findAnchor(
      text,
      dragSelection,
      dragEdge,
      dragCtx,
      dragGhost
    );
    // Bring the anchor into view — with the terminal scrolled back, the
    // selection lives mid-history and the default tail-follow would teleport
    // the reader away from it. When the screen block was located, park the
    // anchor at the same viewport fraction it had in the terminal, so the
    // page reads as "didn't move"; otherwise leave room to grow in the drag
    // direction. Runs even if the button was already released: the reader's
    // place matters either way.
    if (matched) {
      try {
        const r = document.createRange();
        r.setStart(node, Math.min(anchor, text.length));
        r.collapse(true);
        const rr = r.getBoundingClientRect();
        const er = el.getBoundingClientRect();
        const park = frac ?? (dragEdge === "bottom" ? 0.3 : 0.7);
        el.scrollTop += rr.top - (er.top + er.height * park);
      } catch {
        /* keep the tail position */
      }
    }
    if (!buttonDown.current) return; // released before the history arrived
    let live = true;
    let raf = 0;
    // Until the first real mousemove, assume the pointer sits just past the
    // edge that fired, so scrolling starts at once.
    const rect0 = el.getBoundingClientRect();
    let px = rect0.left + rect0.width / 2;
    let py = dragEdge === "bottom" ? rect0.bottom + 24 : rect0.top - 24;
    const extend = () => {
      const r = el.getBoundingClientRect();
      const c = caretAt(
        Math.max(r.left + 4, Math.min(r.right - 4, px)),
        Math.max(r.top + 4, Math.min(r.bottom - 4, py))
      );
      const sel = window.getSelection();
      if (!c || !sel) return;
      try {
        sel.setBaseAndExtent(node, anchor, c.node, c.offset);
      } catch {
        /* caret landed outside the text node tree */
      }
    };
    // Browser-style edge autoscroll: scrolling starts while the pointer is
    // still INSIDE the view but near its edge (EDGE px), speeding up the
    // further past the threshold it goes — holding on the last visible line
    // must keep scrolling, not require pushing outside the pane.
    const EDGE = 24;
    const tick = () => {
      if (!live) return;
      const r = el.getBoundingClientRect();
      const topZone = r.top + EDGE;
      const bottomZone = r.bottom - EDGE;
      if (py < topZone) el.scrollTop -= Math.min(64, 4 + (topZone - py) * 0.3);
      else if (py > bottomZone) el.scrollTop += Math.min(64, 4 + (py - bottomZone) * 0.3);
      extend();
      raf = requestAnimationFrame(tick);
    };
    const onMove = (ev: MouseEvent) => {
      px = ev.clientX;
      py = ev.clientY;
    };
    const stop = () => {
      live = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("mousemove", onMove, true);
      window.removeEventListener("mouseup", finish, true);
    };
    const finish = () => {
      stop();
      suppressElementCopy.current = true;
      setTimeout(() => {
        const s = window.getSelection()?.toString() || "";
        if (s.trim())
          copyText(s).then((ok) => {
            if (ok) toast("Copied " + s.length + " chars");
          });
        suppressElementCopy.current = false;
      }, 0);
    };
    window.addEventListener("mousemove", onMove, true);
    window.addEventListener("mouseup", finish, true);
    raf = requestAnimationFrame(tick);
    return stop;
  }, [text, dragSelection, dragEdge, dragCtx, dragGhost]);

  // Esc closes; Ctrl+↑ / Ctrl+↓ jump to the very top / bottom. Capture-phase
  // so neither the focused xterm nor the global keymap sees the keystrokes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
        e.preventDefault();
        e.stopPropagation();
        const el = scrollRef.current;
        if (el) el.scrollTop = e.key === "ArrowUp" ? 0 : el.scrollHeight;
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  // Same contract as the terminals: drag to select, release to copy.
  const copyOnRelease = () => {
    if (suppressElementCopy.current) return; // continuation already copied
    setTimeout(() => {
      const sel = window.getSelection?.()?.toString() || "";
      if (!sel.trim()) return;
      copyText(sel).then((ok) => {
        if (ok) toast("Copied " + sel.length + " chars");
      });
    }, 0);
  };

  // Click semantics, matching what hands already expect from text: a click
  // with text highlighted just deselects (you stay right where you are); a
  // click with nothing highlighted returns to the live terminal. A drag is
  // never a click (movement check), and the gesture's own release can't
  // trigger this (its mousedown landed on the terminal, so no press start is
  // recorded here).
  const pressAt = useRef<{ x: number; y: number; hadSelection: boolean } | null>(null);
  const onRootMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) {
      pressAt.current = null;
      return;
    }
    const sel = window.getSelection?.();
    const hadSelection = !!(sel && !sel.isCollapsed && sel.toString().trim());
    // Shift+click extends the selection from what's already highlighted to
    // the click point (the release copies it, per the house rule). Done by
    // hand: our collapse-on-press below would otherwise eat the browser's
    // native extension.
    if (e.shiftKey && hadSelection && sel) {
      e.preventDefault();
      const c = caretAt(e.clientX, e.clientY);
      if (c) {
        try {
          sel.extend(c.node, c.offset);
        } catch {
          /* caret outside the text node tree */
        }
      }
      pressAt.current = null; // never a "return" click
      return;
    }
    pressAt.current = {
      x: e.clientX,
      y: e.clientY,
      hadSelection,
    };
    // Pressing on already-selected text would start a native text
    // drag-and-drop, silently keeping the OLD selection (and re-copying it on
    // release). Collapse first so every new drag selects afresh — which is
    // also the "click deselects" half of the contract above.
    sel?.removeAllRanges();
  };
  const onRootMouseUp = (e: React.MouseEvent) => {
    const p = pressAt.current;
    pressAt.current = null;
    if (!p || e.button !== 0) return;
    if (Math.abs(e.clientX - p.x) > 4 || Math.abs(e.clientY - p.y) > 4) return;
    if (p.hadSelection) return; // this click's job was deselecting
    // After the browser settles: if the click somehow produced a selection
    // (double-click word select settling late), it wasn't a "return" click.
    setTimeout(() => {
      const sel = window.getSelection?.();
      if (sel && !sel.isCollapsed && sel.toString().trim()) return;
      onClose();
    }, 0);
  };

  return (
    <div
      className="hist-overlay"
      role="region"
      aria-label="Full pane history"
      onMouseDown={onRootMouseDown}
      onMouseUp={onRootMouseUp}
      onDragStart={(e) => e.preventDefault()}
    >
      <div className="hist-bar">
        <span className="hist-title">
          Full {pane === "shell" ? "terminal" : "agent"} history
        </span>
        <span className="hist-hint">
          drag to select · release to copy · Ctrl+↑/↓ top/bottom · click or Esc returns to live
        </span>
      </div>
      <div className="hist-scroll" ref={scrollRef} onMouseUp={copyOnRelease}>
        {error ? (
          <div className="hist-msg error">{error}</div>
        ) : text === null ? (
          <div className="hist-msg">Loading history…</div>
        ) : text.trim() ? (
          <pre>{text}</pre>
        ) : (
          <div className="hist-msg">No history yet.</div>
        )}
      </div>
    </div>
  );
}
