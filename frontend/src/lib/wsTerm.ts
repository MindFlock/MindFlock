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
  if (!reconnect || attempts >= RETRY_LIMIT) return null;
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
          setState("disconnected");
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
    const obs = new ResizeObserver(() => doFit(true));
    obs.observe(host);
    const t = setTimeout(() => doFit(true), 50);
    connect();

    return () => {
      dead = true;
      clearTimeout(t);
      if (timer) clearTimeout(timer);
      obs.disconnect();
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
