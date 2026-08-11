/** Terminal registry — xterm instances live OUTSIDE React.
 *
 * A terminal must never be torn down by a re-render: each one owns a detached
 * container <div> that a React pane adopts via ref (appendChild moves it
 * without disturbing xterm). Terminals are created on first use, survive
 * re-renders and tab switches, and are disposed only when their pane is
 * explicitly closed (grid membership change) or their session dies.
 *
 * Faithful port of app.js mkTerm + section 13 (mouse-reporting suppression,
 * copy-on-select, SGR wheel synthesis, touch scroll) — see the originals'
 * comments for the full why; the short version: tmux runs `mouse on`, which
 * would normally hand the mouse to the app and break native selection, so we
 * swallow the DECSET mouse enables and synthesize wheel events ourselves. */

import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { toast } from "./toast";
import { copyText, dtHasFiles, pasteClipboard, pasteFilesAsPaths } from "./clipboard";

export type TermKind = "agent" | "shell";

export interface TermHandle {
  term: Terminal;
  container: HTMLDivElement;
  connect(): void;
  doFit(): void;
  dispose(): void;
  /** Send raw data straight to the PTY (voice dictation etc.); false when
   * the socket isn't open. */
  send(data: string): boolean;
  /** Re-assert this viewport's size to tmux even if unchanged on this
   * connection. Sessions run `window-size latest`, so ANOTHER attached
   * client (a second browser, a phone, a stale tab) can yank the window to
   * its own size; the "only send on change" guard in doFit then leaves this
   * pane drawing at the wrong width forever. Called when this client is the
   * one being looked at (focus / tab visible), so the window follows the
   * client you're using instead of the last one to attach. */
  resync(): void;
  /** Live connection state:
   * "connecting" | "connected" | "disconnected" | "error" | "gone".
   * "connecting" is only the pre-first-open state; reconnects stay
   * "disconnected" so status UIs keep signalling a real outage. */
  readonly state: string;
  onState?: (state: string) => void;
  /** Fired once when the first PTY bytes arrive (hide the loading cover). */
  onBoot?: () => void;
  /** True once the first PTY bytes arrived. onBoot fires only once per
   * terminal lifetime, so a pane that remounts (grid drag/reorder) must read
   * this to know the cover should already be hidden. */
  readonly booted: boolean;
  /** Fired when a selection drag pushes past the top or bottom of the
   * terminal — the "keep dragging and more content loads" webpage gesture.
   * The pane answers by opening its full-history overlay (an xterm selection
   * can't extend into tmux history: tmux repaints scrolled content in place,
   * under the highlight's screen-cell anchors). Receives whatever the
   * terminal had selected so far, which edge fired, and a snapshot of the
   * visible screen (plus where the selection's anchor end sits in it), so
   * the overlay can continue the same selection seamlessly — positioned so
   * the content stays where the reader left it — while the button is still
   * held. */
  onHistoryDrag?: (selection: string, edge: "top" | "bottom", ctx?: DragScreenCtx) => void;
  /** Fired (debounced) with the terminal's top visible row after each
   * repaint. The pane watches it for the agent TUI's own pinned-prompt row
   * ("❯ …" while scrolled back) so the desktop prompt bar can mirror that
   * contextual text and mask the duplicate row. */
  onTopRow?: (text: string) => void;
}

/** The visible screen at gesture time: its rows (right-trimmed), and the
 * viewport row/column of the selection end the drag started from. */
export interface DragScreenCtx {
  screen: string[];
  rows: number;
  row: number;
  col: number;
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function cssVar(name: string, fallback: string): string {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch {
    return fallback;
  }
}

export function termTheme() {
  const light = document.documentElement.classList.contains("light");
  return {
    background: cssVar("--term-bg", light ? "#ffffff" : "#0f1117"),
    foreground: cssVar("--term-fg", light ? "#1f2430" : "#d7dae3"),
    cursor: cssVar("--accent", "#7d56f4"),
    selectionBackground: light ? "#b9c0d466" : "#3a415466",
  };
}

/** Re-apply the theme to every live terminal (theme/appearance change). */
export function rethemeAll() {
  const theme = termTheme();
  for (const entry of registry.values()) {
    for (const h of [entry.agent, entry.shell]) {
      if (h) h.term.options.theme = theme;
    }
  }
}

// ---------------------------------------------------------------------------
// Mouse-reporting suppression + copy-on-select + wheel (port of section 13)
// ---------------------------------------------------------------------------

const MOUSE_MODES = [1000, 1001, 1002, 1003, 1004, 1005, 1006, 1015, 1016];

function disableAppMouseReporting(term: Terminal) {
  try {
    const parser = (term as unknown as { parser?: { registerCsiHandler?: Function } }).parser;
    if (!parser?.registerCsiHandler) return;
    const swallow = (params: Array<number | number[]>) => {
      if (!params || !params.length) return false;
      for (const raw of params) {
        const p = typeof raw === "number" ? raw : raw[0];
        if (MOUSE_MODES.indexOf(p) === -1) return false;
      }
      return true; // handled -> xterm's built-in never activates mouse mode
    };
    parser.registerCsiHandler({ prefix: "?", final: "h" }, swallow);
    parser.registerCsiHandler({ prefix: "?", final: "l" }, swallow);
  } catch {
    /* parser API moved: leave defaults */
  }
}

function attachCopyOnSelect(
  host: HTMLElement,
  term: Terminal,
  opts: { session?: string }
) {
  disableAppMouseReporting(term);
  try {
    term.loadAddon(new WebLinksAddon((_ev, uri) => window.open(uri, "_blank")));
  } catch {
    /* link provider unavailable: skip */
  }
  host.title =
    "Drag to select · release to copy · Ctrl+V / right-click pastes (text or image) · wheel scrolls · Ctrl+Shift+C";
  const interactive = !term.options.disableStdin;
  const session = opts.session;
  if (interactive) {
    host.title += " · drop / paste files to upload";
    host.addEventListener(
      "paste",
      (ev: ClipboardEvent) => {
        const cd = ev.clipboardData;
        if (!cd) return; // let xterm's default run
        ev.preventDefault();
        ev.stopPropagation();
        const files = Array.from(cd.files || []);
        const text = cd.getData("text/plain");
        const onlyImages =
          files.length > 0 && files.every((f) => (f.type || "").startsWith("image/"));
        if (files.length && !(text && onlyImages)) {
          pasteFilesAsPaths(files, term, session);
        } else if (text) {
          term.paste(text);
          toast("Pasted " + text.length + " chars");
        } else {
          pasteClipboard(term, session);
        }
      },
      true
    );
    host.addEventListener("dragover", (ev) => {
      if (!dtHasFiles(ev.dataTransfer)) return;
      ev.preventDefault();
      ev.stopPropagation();
      ev.dataTransfer!.dropEffect = "copy";
      host.classList.add("file-drop");
    });
    host.addEventListener("dragleave", (ev) => {
      if (!host.contains(ev.relatedTarget as Node)) host.classList.remove("file-drop");
    });
    host.addEventListener("drop", (ev) => {
      if (!dtHasFiles(ev.dataTransfer)) return;
      ev.preventDefault();
      ev.stopPropagation();
      host.classList.remove("file-drop");
      pasteFilesAsPaths(ev.dataTransfer!.files, term, session);
    });
  }
  // Capture the selection continuously (a TUI repaint can wipe the highlight
  // right after mouse-up) and copy the captured value on release/right-click.
  let captured = "";
  term.onSelectionChange(() => {
    const s = term.getSelection();
    if (s && s.trim()) captured = s;
  });
  function doCopy(): boolean {
    const live = term.getSelection();
    const sel = live && live.trim() ? live : captured;
    if (sel && sel.trim()) {
      copyText(sel).then((ok) => {
        if (ok) toast("Copied " + sel.length + " chars");
      });
      return true;
    }
    return false;
  }
  host.addEventListener("mousedown", (e) => {
    if (e.button === 0) captured = "";
  });
  host.addEventListener("mouseup", (e) => {
    if (e.button === 0) setTimeout(doCopy, 0);
  });
  host.addEventListener("contextmenu", (e) => {
    if (term.options.disableStdin) {
      if (doCopy()) e.preventDefault();
      return;
    }
    e.preventDefault();
    pasteClipboard(term, session);
  });
  term.attachCustomKeyEventHandler((ev) => {
    const mod = ev.ctrlKey || ev.metaKey;
    if (ev.type === "keydown" && mod && ev.shiftKey && (ev.key === "C" || ev.key === "c")) {
      if (doCopy()) {
        ev.preventDefault();
        return false;
      }
    }
    // Ctrl+V: don't forward ^V to the PTY — returning false WITHOUT
    // preventDefault lets the browser fire native paste on xterm's textarea;
    // the capture-phase paste listener above routes it.
    if (
      ev.type === "keydown" &&
      mod &&
      !ev.shiftKey &&
      !ev.altKey &&
      (ev.key === "v" || ev.key === "V") &&
      !term.options.disableStdin
    ) {
      return false;
    }
    // Let native (centered) zoom handle Ctrl/Cmd +/-/0.
    if (mod && !ev.altKey) {
      const c = ev.code;
      if (
        c === "Equal" ||
        c === "Minus" ||
        c === "Digit0" ||
        c === "NumpadAdd" ||
        c === "NumpadSubtract" ||
        c === "Numpad0"
      ) {
        return false;
      }
    }
    return true;
  });
}

// Wheel speed: thirds of a line (0.33…3); tmux -N counts whole lines, so the
// settings loader stores the residual ratio here (see Settings → scroll speed).
let wheelDamping = 1;
export function setWheelDamping(speed: number) {
  wheelDamping = speed / Math.max(1, Math.round(speed));
}

function attachWheelScroll(host: HTMLElement, term: Terminal, getWs: () => WebSocket | null) {
  function cellHeight() {
    return host.getBoundingClientRect().height / (term.rows || 24) || 16;
  }
  function sendWheel(clientX: number, clientY: number, up: boolean, count: number): boolean {
    const ws = getWs();
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    const rect = host.getBoundingClientRect();
    const cols = term.cols || 80,
      rows = term.rows || 24;
    const cellW = rect.width / cols || 8,
      cellH = rect.height / rows || 16;
    const col = Math.max(1, Math.min(cols, Math.floor((clientX - rect.left) / cellW) + 1));
    const row = Math.max(1, Math.min(rows, Math.floor((clientY - rect.top) / cellH) + 1));
    const btn = up ? 64 : 65;
    let seq = "";
    for (let i = 0; i < count; i++) seq += "\x1b[<" + btn + ";" + col + ";" + row + "M";
    ws.send(seq);
    return true;
  }
  let wheelAcc = 0,
    wheelAt = 0;
  host.addEventListener(
    "wheel",
    (ev) => {
      if (!ev.deltaY) return;
      const ws = getWs();
      if (!ws || ws.readyState !== WebSocket.OPEN) return; // no PTY -> xterm scrolls
      const raw = ev.deltaMode === 1 ? ev.deltaY : ev.deltaY / cellHeight();
      const now = ev.timeStamp || performance.now();
      if (now - wheelAt > 250 || (wheelAcc && Math.sign(wheelAcc) !== Math.sign(raw)))
        wheelAcc = 0;
      wheelAt = now;
      wheelAcc += raw * wheelDamping;
      const lines = Math.min(8, Math.floor(Math.abs(wheelAcc)));
      if (lines) {
        const up = wheelAcc < 0;
        wheelAcc += up ? lines : -lines;
        sendWheel(ev.clientX, ev.clientY, up, lines);
      }
      ev.preventDefault();
      ev.stopPropagation();
    },
    { capture: true, passive: false }
  );

  // Touch: translate a clearly-vertical one-finger drag into SGR wheel ticks
  // (natural-scrolling direction); taps still focus, horizontal is untouched.
  host.style.touchAction = "none";
  let touchLast: { x: number; y: number } | null = null;
  let touchScrolling = false;
  host.addEventListener(
    "touchstart",
    (ev) => {
      if (ev.touches.length !== 1) {
        touchLast = null;
        return;
      }
      touchLast = { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
      touchScrolling = false;
    },
    { capture: true, passive: true }
  );
  host.addEventListener(
    "touchmove",
    (ev) => {
      if (!touchLast || ev.touches.length !== 1) return;
      const t = ev.touches[0];
      const dy = t.clientY - touchLast.y,
        dx = t.clientX - touchLast.x;
      if (!touchScrolling) {
        if (Math.abs(dy) < 8 || Math.abs(dy) <= Math.abs(dx)) return;
        touchScrolling = true;
      }
      const TOUCH_SCROLL_MULT = 3; // 1:1 finger-to-line feels glacial (see mobile.js)
      const lines = Math.floor((Math.abs(dy) * TOUCH_SCROLL_MULT) / cellHeight());
      if (lines > 0) {
        if (!sendWheel(t.clientX, t.clientY, dy > 0, Math.min(24, lines))) return;
        touchLast = { x: t.clientX, y: t.clientY };
      }
      ev.preventDefault();
      ev.stopPropagation();
    },
    { capture: true, passive: false }
  );
  host.addEventListener(
    "touchend",
    () => {
      touchLast = null;
      touchScrolling = false;
    },
    { capture: true, passive: true }
  );
}

// Drag-past-the-edge → history gesture. A left-button drag that starts in
// the terminal and holds past its top (or bottom) edge for a beat reads as
// "keep scrolling, I want more than the screen holds" — the browser gesture
// for extending a selection beyond the viewport. We can't honor it in xterm
// (tmux repaints history in place, so the selection anchors point at
// rewritten cells); instead it fires once per drag and the pane opens the
// full-history overlay. The hold delay keeps an overshoot while selecting an
// edge line from triggering it.
function attachDragHistoryGesture(
  host: HTMLElement,
  fire: (edge: "top" | "bottom") => void
) {
  const HOLD_MS = 220;
  const MARGIN = 8; // px past the top edge before the hold timer arms
  // The bottom zone starts INSIDE the terminal: its last visible row is the
  // tmux status bar, so a downward selection drag naturally comes to rest ON
  // that row, never 8px past the container — requiring "outside only" made
  // the downward gesture nearly impossible to hit in practice.
  const BOTTOM_ZONE = 18;
  host.addEventListener("mousedown", (down: MouseEvent) => {
    if (down.button !== 0) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onMove = (ev: MouseEvent) => {
      const r = host.getBoundingClientRect();
      const edge: "top" | "bottom" | null =
        ev.clientY < r.top - MARGIN
          ? "top"
          : ev.clientY > r.bottom - BOTTOM_ZONE
            ? "bottom"
            : null;
      if (edge && timer === undefined) {
        timer = setTimeout(() => {
          cleanup();
          fire(edge);
        }, HOLD_MS);
      } else if (!edge && timer !== undefined) {
        clearTimeout(timer);
        timer = undefined;
      }
    };
    const cleanup = () => {
      if (timer !== undefined) clearTimeout(timer);
      window.removeEventListener("mousemove", onMove, true);
      window.removeEventListener("mouseup", cleanup, true);
    };
    window.addEventListener("mousemove", onMove, true);
    window.addEventListener("mouseup", cleanup, true);
  });
}

// ---------------------------------------------------------------------------
// Terminal handle (port of mkTerm) + registry
// ---------------------------------------------------------------------------

function makeTerm(
  title: string,
  wsPath: string,
  interactive: boolean,
  // When set, the websocket connects to this absolute path instead of the
  // per-instance "/api/instances/{title}{wsPath}" template — used by
  // standalone terminals (provider login) that aren't tied to a session.
  absolutePath?: string
): TermHandle {
  const container = document.createElement("div");
  container.className = "term-container";
  const term = new Terminal({
    cursorBlink: interactive,
    fontSize: 13,
    theme: termTheme(),
    disableStdin: !interactive,
    fontFamily: 'ui-monospace, "Cascadia Code", Menlo, Consolas, monospace',
    // 2000 lines: full history lives in tmux ("Copy all" → /history); with up
    // to 9 panes × 2 terminals, oversized buffers were the dominant heap cost.
    scrollback: 2000,
    macOptionClickForcesSelection: true,
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(container);
  attachCopyOnSelect(container, term, { session: title });
  // Clicking into a terminal makes this client the one in use — claim the
  // tmux window size for it (see resync).
  container.addEventListener("focusin", () => handle.resync());
  attachDragHistoryGesture(container, (edge) => {
    let ctx: DragScreenCtx | undefined;
    try {
      const buf = term.buffer.active;
      const rows = term.rows || 24;
      const screen: string[] = [];
      for (let r = 0; r < rows; r++)
        screen.push(buf.getLine(buf.viewportY + r)?.translateToString(true) ?? "");
      // The anchor end of the selection — where the drag STARTED: its top
      // end when dragging down (bottom edge), its bottom end dragging up.
      const p = term.getSelectionPosition();
      const side = edge === "bottom" ? p?.start : p?.end;
      const row = side
        ? Math.max(0, Math.min(rows - 1, side.y - buf.viewportY))
        : edge === "bottom"
          ? 0
          : rows - 1;
      ctx = { screen, rows, row, col: side?.x ?? 0 };
    } catch {
      /* buffer API moved: the overlay falls back to selection matching */
    }
    handle.onHistoryDrag?.(term.getSelection() || "", edge, ctx);
  });
  // Top-row watcher (see TermHandle.onTopRow). THROTTLED, not debounced: a
  // streaming turn renders continuously, and a trailing debounce would never
  // settle — the watcher then missed the TUI's pinned row for the whole
  // stream. Leading edge fires immediately; the trailing timer catches the
  // final state after a burst.
  let topRowAt = 0;
  let topRowTimer: ReturnType<typeof setTimeout> | undefined;
  const emitTopRow = () => {
    if (!handle.onTopRow) return;
    try {
      const buf = term.buffer.active;
      handle.onTopRow(buf.getLine(buf.viewportY)?.translateToString(true) ?? "");
    } catch {
      /* buffer API moved */
    }
  };
  term.onRender(() => {
    const now = performance.now();
    clearTimeout(topRowTimer);
    if (now - topRowAt > 150) {
      topRowAt = now;
      emitTopRow();
    } else {
      topRowTimer = setTimeout(() => {
        topRowAt = performance.now();
        emitTopRow();
      }, 160);
    }
  });

  let ws: WebSocket | null = null;
  let sentSize = "";
  let closed = false;
  let booted = false;
  let reconnectAttempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let state = "connecting";

  const handle: TermHandle & { _setState(s: string): void } = {
    term,
    container,
    get state() {
      return state;
    },
    get booted() {
      return booted;
    },
    _setState(s: string) {
      state = s;
      handle.onState?.(s);
      notifyTermStates();
    },
    send(data: string) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(data);
        return true;
      }
      return false;
    },
    resync() {
      sentSize = "";
      handle.doFit();
    },
    doFit() {
      // Skip while hidden/zero-sized — fitting then locks in a 1x1 grid.
      if (!container.clientWidth || !container.clientHeight || container.offsetParent === null)
        return;
      try {
        fit.fit();
      } catch {
        return;
      }
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      // Resize tmux only when the size CHANGED on this connection: sessions
      // run window-size latest, and unconditional sends make clients with
      // different viewports endlessly yank the windows back and forth.
      const size = term.cols + "x" + term.rows;
      if (sentSize === size) return;
      sentSize = size;
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    },
    connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const path =
        absolutePath ?? "/api/instances/" + encodeURIComponent(title) + wsPath;
      const sock = new WebSocket(proto + "://" + location.host + path);
      sock.binaryType = "arraybuffer";
      ws = sock;
      sentSize = ""; // fresh attach (PTY spawns 24x80) always gets one real size
      sock.onopen = () => {
        reconnectAttempt = 0;
        handle._setState("connected");
        handle.doFit();
      };
      sock.onmessage = (ev) => {
        if (!booted) {
          booted = true;
          handle.onBoot?.();
        }
        if (typeof ev.data === "string") {
          try {
            const j = JSON.parse(ev.data);
            if (j.type === "error") {
              term.write("\r\n[error] " + j.message + "\r\n");
              return;
            }
          } catch {
            term.write(ev.data);
          }
        } else {
          term.write(new Uint8Array(ev.data));
        }
      };
      sock.onclose = (ev) => {
        handle._setState("disconnected");
        if (closed) return;
        // 4404 = instance gone, 4409 = workspace gone -> stop retrying.
        if (ev && (ev.code === 4404 || ev.code === 4409)) {
          closed = true;
          handle._setState("gone");
          if (!booted) {
            booted = true;
            handle.onBoot?.();
          }
          return;
        }
        // Backoff: probe fast first (0.4s), ease out to the 2.5s cap — on a
        // restore the backend may need a beat to relaunch the agent.
        const n = (reconnectAttempt += 1);
        const delay = Math.min(2500, 400 * Math.pow(2, n - 1));
        reconnectTimer = setTimeout(() => {
          if (!closed) handle.connect();
        }, delay);
      };
      sock.onerror = () => handle._setState("error");
    },
    dispose() {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
      try {
        term.dispose();
      } catch {
        /* already disposed */
      }
      container.remove();
    },
  };

  if (interactive) {
    term.onData((data) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
    });
    attachWheelScroll(container, term, () => ws);
  }
  return handle;
}

interface RegistryEntry {
  agent?: TermHandle;
  shell?: TermHandle;
}

const registry = new Map<string, RegistryEntry>();

// Connection-state subscription for components OUTSIDE the owning pane (the
// sidebar dots). onState is a single slot the pane claims on adopt, so other
// listeners subscribe here; fired on every _setState and on registry removal.
const termStateListeners = new Set<() => void>();

export function subscribeTermStates(cb: () => void): () => void {
  termStateListeners.add(cb);
  return () => {
    termStateListeners.delete(cb);
  };
}

function notifyTermStates() {
  for (const cb of termStateListeners) cb();
}

/** Get (creating + connecting on first use) a session's terminal. */
export function getTerm(title: string, kind: TermKind): TermHandle {
  let entry = registry.get(title);
  if (!entry) {
    entry = {};
    registry.set(title, entry);
  }
  let h = entry[kind];
  if (!h) {
    h = makeTerm(title, kind === "agent" ? "/terminal" : "/shell", true);
    h.connect();
    entry[kind] = h;
  }
  return h;
}

export function peekTerm(title: string, kind: TermKind): TermHandle | undefined {
  return registry.get(title)?.[kind];
}

/** Dispose a session's terminals (pane closed / session killed). */
export function releaseTerms(title: string) {
  const entry = registry.get(title);
  if (!entry) return;
  entry.agent?.dispose();
  entry.shell?.dispose();
  registry.delete(title);
  notifyTermStates();
}

/** Drop terminals whose sessions no longer exist. */
export function pruneTerms(liveTitles: Set<string>) {
  for (const title of [...registry.keys()]) {
    if (!liveTitles.has(title)) releaseTerms(title);
  }
}

export function fitAll() {
  for (const entry of registry.values()) {
    entry.agent?.doFit();
    entry.shell?.doFit();
  }
}

/** Re-assert every live terminal's size (see TermHandle.resync). */
export function resyncAll() {
  for (const entry of registry.values()) {
    entry.agent?.resync();
    entry.shell?.resync();
  }
}

// Coming back to this tab makes it the client in use: reclaim the tmux window
// size for it. Only one tab is visible at a time, so this can't ping-pong the
// way an unconditional periodic re-send would.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) resyncAll();
  });
}

/** Focus the logical terminal for a session (agent pane by default). */
export function focusTerm(title: string, kind: TermKind = "agent") {
  peekTerm(title, kind)?.term.focus();
}
