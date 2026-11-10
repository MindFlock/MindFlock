/** A ws-streamed xterm bound to a host div — the one terminal implementation
 * two very different surfaces share.
 *
 * Lifted out of `SpecialPane` when the Verify dialog started needing it. A
 * verify run is minutes of an agent working a checklist, and watching it used to
 * mean closing the whole Verify dialog so a grid pane could be opened behind it
 * — which threw away the checklist you were reading, on the one surface where
 * the checklist IS the point. The dialog now renders the same stream inline, so
 * "watch the agent" and "answer the steps it left you" are one screen instead of
 * two windows and a round trip.
 *
 * One copy rather than two, because the interesting parts are all failure
 * handling — the resize handshake, the JSON-or-bytes frame split, the disposal
 * order, and the reconnect below — and a second copy is how one of them
 * silently stops matching.
 */

import { useEffect, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { termTheme } from "./terminals";
import { attachFileDrop } from "./clipboard";

/** Close code `terminal_ws` sends when the engine has no such session YET.
 *
 * Not an error and usually not even a mistake: `create_instance` registers the
 * session and returns, then does the real work — cutting a worktree, installing
 * dependencies, starting tmux — on a background task that can take minutes. A
 * socket opened in that window is told 4404 and is right to come back. */
const NOT_YET = 4404;

/** ...and when the session exists but its agent pane could not be attached
 * (tmux still starting, or the agent being rebooted). Also transient. */
const NOT_READY = 4409;

/** ...and when the session is PAUSED: its agent was stopped on purpose and the
 * server will not relaunch it on a socket (only Resume does). Final — retrying
 * would just be refused again. */
export const PAUSED_CLOSE = 4423;

/** ...and when the session is HELD offline after a crash (backend
 * core/held.py): its agent died and nothing starts it but a person's Start.
 * Final like Paused — a retry is exactly the relaunch-on-touch the hold
 * exists to stop. */
export const HELD_CLOSE = 4424;

/** Backoff between attempts, in ms. Flat after the third: the thing being waited
 * on is a worktree checkout, which takes as long as it takes, and a doubling
 * curve would be checking every two minutes by the time it finished. */
const RETRY_MS = [700, 1500, 3000];
const RETRY_MAX_MS = 3000;

/** How long to keep trying before admitting it is not coming (~5 minutes).
 * Long, because the worst case this exists for — a first worktree on a cold
 * cache — genuinely is minutes; bounded, because a socket retrying forever
 * against a session that will never exist is a page that never goes quiet. */
const RETRY_LIMIT = 100;

/** What to do when a socket closes: how long to wait, or `null` to give up.
 *
 * Pulled out as a pure function because it is the only part of this file with a
 * decision in it, and because `frontend/vitest.config` is node-only by design —
 * there is no DOM and no WebSocket in the test environment, so a rule left
 * inside the effect is a rule nothing can check. The bug this encodes (a run's
 * first socket is always refused, and the old code called that failure) is
 * exactly the kind that comes back.
 *
 * `attempts` is how many retries have already happened, so the first close is
 * `0`.
 */
export function retryAfter(
  code: number,
  attempts: number,
  reconnect: boolean,
): number | null {
  if (!reconnect || attempts >= RETRY_LIMIT || code === PAUSED_CLOSE || code === HELD_CLOSE) return null;
  // The session is not up YET — the normal path for the first seconds of every
  // run, and never a failure.
  if (code === NOT_YET || code === NOT_READY)
    return RETRY_MS[attempts] ?? RETRY_MAX_MS;
  // An abnormal drop (1006) or a server going away (1001) is worth one look —
  // usually the server restarting under a live session. Anything else, once we
  // have already been connected, is the far end saying no on purpose.
  if (code === 1006 || code === 1001 || attempts === 0)
    return RETRY_MS[attempts] ?? RETRY_MAX_MS;
  return null;
}

/** Whether a close code means "not up yet" rather than "went away". Only the
 * wording the user sees depends on it. */
export function isStarting(code: number): boolean {
  return code === NOT_YET || code === NOT_READY;
}

/** How long the "<name> is driving" line stays up, in ms. */
export const NOTICE_MS = 3000;

/** One line drawn over the bottom of a terminal — "Alice is driving — take
 * control to type" when a watcher's keystroke was dropped. An overlay, never
 * written into the xterm: the screen belongs to the agent's TUI, and a line
 * printed into it would be painted over (or worse, scroll it). Reuses one
 * element per host and restarts its timer, so a burst of keystrokes is one
 * notice. */
export function showTermNotice(host: HTMLElement, text: string): void {
  let el = host.querySelector<HTMLElement>(":scope > .term-notice");
  if (!el) {
    el = document.createElement("div");
    el.className = "term-notice";
    el.setAttribute("role", "status");
    if (getComputedStyle(host).position === "static") host.style.position = "relative";
    host.appendChild(el);
  }
  el.textContent = text;
  el.classList.remove("gone");
  const prev = (el as HTMLElement & { _t?: ReturnType<typeof setTimeout> })._t;
  if (prev) clearTimeout(prev);
  (el as HTMLElement & { _t?: ReturnType<typeof setTimeout> })._t = setTimeout(
    () => el!.classList.add("gone"),
    NOTICE_MS,
  );
}

/** A session terminal's own control frames (backend core.ownership), or null
 * for anything else: `{"type": "readonly", "message"}` — a keystroke was
 * dropped because another person drives (show `message`); `{"type":
 * "driver", "driving", "driver", "driver_name"}` — who holds the keyboard,
 * sent on attach and on every change (the pane header reads the same fact
 * from the session row, so nothing needs drawing). */
export function controlNotice(frame: unknown): { notice: string } | { notice: null } | null {
  if (!frame || typeof frame !== "object") return null;
  const j = frame as { type?: unknown; message?: unknown };
  if (j.type === "readonly") return { notice: typeof j.message === "string" ? j.message : "" };
  if (j.type === "driver") return { notice: null };
  return null;
}

// ---------------------------------------------------------------------------
// Repaint: the "pane went white until I scrolled" fix (docs/web-ui.md)
// ---------------------------------------------------------------------------
//
// A terminal pane could turn blank — white, on the light theme, where the
// terminal background IS white — and come back on the smallest scroll. The
// content was never lost: a wheel tick makes tmux redraw, xterm rewrites its
// row elements, and that rewrite is what makes Chromium paint the pane again.
// Nothing else ever did. xterm 6 renders with the DOM renderer here (no WebGL
// addon, so no GL context to lose); what goes stale is the PAINTED surface:
// the window was minimized, covered (Windows' native occlusion tracking hides
// the page), moved to another display, or the GPU process restarted (the
// owner switches the laptop dGPU off and on), and the raster of a pane that
// had no new output was never redone. Each of those now ends in a repaint of
// every terminal on screen: fit (in case the size changed meanwhile) plus a
// full `refresh` — exactly the row rewrite the scroll used to provoke.

/** A terminal surface that can be repainted. Kept to the two xterm members
 * used so the tests can hand in fakes (vitest is node-only: no DOM, no xterm). */
export interface RepaintTerm {
  readonly rows: number;
  refresh(start: number, end: number): void;
}

/** Fit (when given) then redraw every row of `term`. Never throws: a terminal
 * disposed between the trigger and the frame is simply skipped. */
export function repaintTerm(term: RepaintTerm | null | undefined, fit?: () => void): boolean {
  if (!term) return false;
  try {
    fit?.();
  } catch {
    /* hidden / zero-sized: the refresh below still repaints what is there */
  }
  try {
    const rows = term.rows;
    if (!(rows > 0)) return false;
    term.refresh(0, rows - 1);
    return true;
  } catch {
    return false;
  }
}

/** Whether an element is on screen enough to be worth painting: laid out
 * (not `display: none` itself or by an ancestor) and with an area. */
export function isShown(el: { clientWidth: number; clientHeight: number; offsetParent: unknown } | null | undefined): boolean {
  return !!el && el.clientWidth > 0 && el.clientHeight > 0 && el.offsetParent !== null;
}

/** One terminal the repaint hub knows: whether it is showing now, and how to
 * repaint it. Both are read at repaint time, so a pane that switched tab or
 * reconnected (a new xterm) is handled by the same entry. */
export interface Repaintable {
  shown(): boolean;
  repaint(): void;
}

const tracked = new Set<Repaintable>();

/** Put a terminal on the repaint list; returns the untrack function. */
export function trackRepaint(r: Repaintable): () => void {
  tracked.add(r);
  return () => {
    tracked.delete(r);
  };
}

/** Repaint every entry that is showing; hidden ones are skipped (xterm repaints
 * them itself when they are revealed, and the pane's reveal refit repaints them
 * again). Returns how many were repainted. One bad entry never stops the rest. */
export function repaintShown(items: Iterable<Repaintable> = tracked): number {
  let n = 0;
  for (const r of items) {
    try {
      if (!r.shown()) continue;
      r.repaint();
      n++;
    } catch {
      /* torn down mid-pass */
    }
  }
  return n;
}

/** The event the desktop shell (electron/main.js) dispatches on the page when
 * the native window is restored, shown, un-occluded, moved between displays,
 * or the GPU process came back — the cases a page cannot see for itself. */
export const REPAINT_EVENT = "mindflock:repaint";

/** Second pass after a trigger, in ms: Chromium can show a restored window a
 * frame or two before its compositor is ready, and a single repaint landing
 * in that gap is lost the same way the original paint was. */
export const REPAINT_SETTLE_MS = 300;

type Listenable = {
  addEventListener(type: string, fn: () => void): void;
  removeEventListener(type: string, fn: () => void): void;
};

/** Wire the triggers: the window regaining focus, the page becoming visible
 * again (minimize/restore, a background tab, occlusion), a bfcache restore,
 * and the shell's own REPAINT_EVENT. Bursts (focus + visibilitychange arrive
 * together) coalesce into one pass on the next frame, plus one settle pass.
 * Returns the uninstall function. Dependencies are parameters for the test. */
export function installRepaintTriggers(
  win: Listenable,
  doc: Listenable & { readonly hidden: boolean },
  run: () => void = () => {
    repaintShown();
  },
  schedule: {
    frame: (fn: () => void) => void;
    later: (fn: () => void, ms: number) => unknown;
    cancel: (t: unknown) => void;
  } = {
    frame: (fn) => requestAnimationFrame(() => fn()),
    later: (fn, ms) => setTimeout(fn, ms),
    cancel: (t) => clearTimeout(t as ReturnType<typeof setTimeout>),
  },
): () => void {
  let queued = false;
  let settle: unknown;
  const trigger = () => {
    if (doc.hidden) return; // nothing is painted while hidden; the show will come
    if (!queued) {
      queued = true;
      schedule.frame(() => {
        queued = false;
        run();
      });
    }
    if (settle !== undefined) schedule.cancel(settle);
    settle = schedule.later(() => {
      settle = undefined;
      run();
    }, REPAINT_SETTLE_MS);
  };
  const winEvents = ["focus", "pageshow", REPAINT_EVENT];
  for (const e of winEvents) win.addEventListener(e, trigger);
  doc.addEventListener("visibilitychange", trigger);
  return () => {
    for (const e of winEvents) win.removeEventListener(e, trigger);
    doc.removeEventListener("visibilitychange", trigger);
    if (settle !== undefined) schedule.cancel(settle);
  };
}

/** A ws-streamed terminal over a fixed path.
 *
 * `reconnect` makes the socket survive a session that is not up yet. It is off
 * by default so the grid panes keep their existing behaviour, and ON for the
 * Verify dialog's inline watcher, which is the surface that needs it:
 *
 * THE BUG IT FIXES. Pressing Run mounts the watcher immediately — that is the
 * whole point of it, you press the button and see the agent — but the session it
 * names does not exist yet, because `create_instance` returns as soon as the
 * record is registered and does the worktree and the tmux launch in the
 * background. So the first socket was refused with 4404, the hook recorded
 * "disconnected", and it stayed that way: a blank white box with a dead label,
 * every single time, for the one control the feature is judged by. Nothing was
 * actually wrong — the run was starting normally — and there was no way to tell
 * from the screen.
 *
 * The state string is rendered to the user, so it says which of the two
 * situations it is in rather than reporting every one of them as failure.
 */
export function useWsTerm(
  hostRef: React.RefObject<HTMLDivElement | null>,
  wsPath: string,
  interactive: boolean,
  reconnect = false,
) {
  const [state, setState] = useState("connecting");
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new Terminal({
      cursorBlink: interactive,
      fontSize: 12,
      theme: termTheme(),
      disableStdin: !interactive,
      fontFamily: 'ui-monospace, "Cascadia Code", Menlo, Consolas, monospace',
      scrollback: 20000,
      macOptionClickForcesSelection: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);

    // A file dropped (or an image pasted) on a window that TAKES INPUT becomes
    // a path the agent can read — the same gesture, and the same helper, as a
    // session terminal. It was missing here, which is worse than absent: the
    // assistant is a chat you hand things to, and dropping a screenshot on it
    // did nothing while dropping one on the pane beside it worked. Interactive
    // only: the log tails and a verify watch have no PTY to paste a path into.
    //
    // No session is passed, so uploads land in `~/.mindflock/pastes` rather
    // than a workspace — the assistant has no worktree of its own, and the
    // path handed over is absolute either way.
    const detachFiles = interactive ? attachFileDrop(host, term) : undefined;
    if (interactive) host.title = "Drop or paste a file to hand over its path";

    // THE TERMINAL OUTLIVES THE SOCKET. Only the WebSocket is rebuilt on a
    // retry; recreating the xterm with it would clear the scrollback every
    // time, which on a reconnecting session means losing the output you were
    // reading in order to go on reading it.
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let dead = false; // the effect has been torn down
    let attempts = 0;
    let said = false; // the "waiting" line is written once, not once per try

    const doFit = (sendResize: boolean) => {
      try {
        fit.fit();
      } catch {
        return;
      }
      if (sendResize && ws && ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    };

    const connect = () => {
      if (dead) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const sock = new WebSocket(proto + "://" + location.host + wsPath);
      ws = sock;
      sock.binaryType = "arraybuffer";
      sock.onopen = () => {
        if (dead) return;
        attempts = 0;
        setState("streaming");
        doFit(true);
      };
      sock.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          try {
            const j = JSON.parse(ev.data);
            if (j.type === "error") {
              term.write("\r\n[error] " + j.message + "\r\n");
              return;
            }
            const ctl = controlNotice(j);
            if (ctl) {
              if (ctl.notice) showTermNotice(host, ctl.notice);
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
        if (dead) return;
        const wait = retryAfter(ev.code, attempts, reconnect);
        if (wait === null) {
          setState(ev.code === PAUSED_CLOSE ? "paused" : ev.code === HELD_CLOSE ? "held" : "disconnected");
          return;
        }
        const transient = isStarting(ev.code);
        if (!said) {
          said = true;
          // Said IN THE TERMINAL, because the terminal is the thing the user is
          // staring at: a blank box with a word above it reads as broken, and
          // this is the normal path for the first ten seconds of every run.
          term.write(
            "\r\n\x1b[2mWaiting for the session to start — this takes a moment " +
              "the first time in a repository.\x1b[0m\r\n",
          );
        }
        setState(transient ? "starting" : "reconnecting");
        timer = setTimeout(connect, wait);
        attempts++;
      };
      sock.onerror = () => {
        // `onclose` always follows, and it is the one that knows the code.
      };
    };

    if (interactive)
      term.onData((d) => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(d);
      });
    const obs = new ResizeObserver(() => {
      doFit(true);
      repaintTerm(term);
    });
    obs.observe(host);
    const t = setTimeout(() => doFit(true), 50);
    // Repainted with the grid's panes when the window comes back (see
    // installRepaintTriggers): the Verify watcher can go white the same way.
    const untrack = trackRepaint({
      shown: () => isShown(host),
      repaint: () => repaintTerm(term, () => doFit(true)),
    });
    connect();

    return () => {
      dead = true;
      untrack();
      clearTimeout(t);
      if (timer) clearTimeout(timer);
      obs.disconnect();
      // Removed by hand: the host div is React's and OUTLIVES this effect, so a
      // re-run would stack a second set of listeners on it and upload every
      // dropped file twice.
      detachFiles?.();
      try {
        ws?.close();
      } catch {
        /* closed */
      }
      term.dispose();
    };
  }, [hostRef, wsPath, interactive, reconnect]);
  return state;
}
