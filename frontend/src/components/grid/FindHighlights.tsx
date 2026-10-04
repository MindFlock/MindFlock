/** Find highlights painted over a live terminal (scroll-mode Ctrl+F).
 *
 * When the pane's app scrolls itself, the server scrolls it to each hit
 * (backend pane_scroll_find) but nothing paints the hits — tmux does that
 * only for its own copy-mode. So this layer does, like a browser's find:
 * every visible hit yellow, the current one orange. It re-reads the screen on
 * every repaint, so boxes follow whatever the app draws (a streaming reply,
 * the next scroll) rather than going stale. Only rows inside the scrolling
 * region count — never the app's input box or status bar. */

import { useEffect, useRef, useState } from "react";
import type { Terminal } from "@xterm/xterm";
import { rowHits, type RowCell } from "../../lib/screenFind";

interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
  cur: boolean;
}

export function FindHighlights({
  term,
  patterns,
  region,
  current,
}: {
  term: Terminal;
  /** Every term to paint (the query, and a proximity search's second one). */
  patterns: RegExp[];
  /** Scrolling-region rows from the server; nothing is painted until known. */
  region: [number, number] | null;
  /** The current hit's spans as (row, code-point column, length): the hit,
   * and its proximity partner when that's on screen. */
  current: Array<[number, number, number]>;
}) {
  const [boxes, setBoxes] = useState<Box[]>([]);
  const layerRef = useRef<HTMLDivElement | null>(null);
  const lo = region?.[0];
  const hi = region?.[1];
  const curKey = current.map((c) => c.join(",")).join(";");
  const patKey = patterns.map((p) => p.source + "/" + p.flags).join("\n");

  useEffect(() => {
    let raf = 0;
    const compute = () => {
      raf = 0;
      const layer = layerRef.current;
      const screenEl = term.element?.querySelector(".xterm-screen") as HTMLElement | null;
      if (!layer || !screenEl || lo == null || hi == null || !layer.parentElement) {
        setBoxes([]);
        return;
      }
      const host = layer.parentElement.getBoundingClientRect();
      const sr = screenEl.getBoundingClientRect();
      const cw = sr.width / (term.cols || 80);
      const chh = sr.height / (term.rows || 24);
      const buf = term.buffer.active;
      const out: Box[] = [];
      for (let r = lo; r <= Math.min(hi, term.rows - 1); r++) {
        const line = buf.getLine(buf.viewportY + r);
        if (!line) continue;
        const cells: RowCell[] = [];
        for (let x = 0; x < line.length; x++) {
          const c = line.getCell(x);
          if (!c) continue;
          const w = c.getWidth();
          if (w === 0) continue; // the trailing half of a wide character
          cells.push({ ch: c.getChars(), x, w });
        }
        const seen = new Set<string>();
        for (const h of patterns.flatMap((pat) => rowHits(cells, pat))) {
          const k = h.start + ":" + h.len;
          if (seen.has(k)) continue;
          seen.add(k);
          out.push({
            left: sr.left - host.left + h.x * cw,
            top: sr.top - host.top + r * chh,
            width: h.width * cw,
            height: chh,
            cur: current.some(([cr, cc]) => cr === r && cc === h.start),
          });
        }
      }
      setBoxes(out);
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(compute);
    };
    schedule();
    const sub = term.onRender(schedule);
    window.addEventListener("resize", schedule);
    return () => {
      sub.dispose();
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", schedule);
    };
    // patKey/curKey stand in for the arrays (new identities every render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [term, patKey, lo, hi, curKey]);

  return (
    <div className="find-hl-layer" ref={layerRef} aria-hidden="true">
      {boxes.map((b, i) => (
        <div
          key={i}
          className={"find-hl" + (b.cur ? " cur" : "")}
          style={{ left: b.left, top: b.top, width: b.width, height: b.height }}
        />
      ))}
    </div>
  );
}
