/** Re-exec the server and wait for it to answer again.
 *
 * `POST /api/server/restart` responds and *then* re-execs, so there is no
 * "restarted" event to await — the only honest signal is the next request
 * that succeeds. Hence the poll: stay busy until a cheap endpoint answers,
 * and give up after a bounded wait rather than spinning forever.
 *
 * Shared by Settings → Mobile (restart to apply a serve-mode change) and
 * Settings → Advanced (restart on demand).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";

/** How long to keep polling before assuming the server isn't coming back. */
const RESTART_TIMEOUT_MS = 30000;
const POLL_INTERVAL_MS = 1000;

export interface RestartOptions {
  /** Reload the page once the server answers again, restarting the UI too.
   *
   * Deliberately *after* the poll succeeds: reloading immediately would race
   * the re-exec and land on a dead port, which in the desktop app means the
   * offline page instead of a fresh UI. On timeout we never reload — leaving
   * the stale-but-working UI up beats navigating into a hole. */
  reload?: boolean;
  /** Runs once the server answers again. Skipped when `reload` wins, since
   * the page is on its way out. */
  onBack?(): void;
}

export interface ServerRestart {
  /** True from the click until the server answers again (or we time out). */
  restarting: boolean;
  /** Set when the wait timed out — the server may still be down. */
  timedOut: boolean;
  /** Fire the restart. */
  restart(opts?: RestartOptions): void;
}

export function useServerRestart(): ServerRestart {
  const [restarting, setRestarting] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  // A restart outliving its screen would leave an interval running against an
  // unmounted component, so drop it on the way out.
  useEffect(() => () => {
    if (timer.current) clearInterval(timer.current);
  }, []);

  const restart = useCallback((opts?: RestartOptions) => {
    if (timer.current) clearInterval(timer.current);
    setRestarting(true);
    setTimedOut(false);
    // Fire-and-forget: the response races the re-exec, so a transport error
    // here means nothing. The poll below is what actually reports the outcome.
    api("/api/server/restart", { method: "POST" }).catch(() => {});
    const t0 = Date.now();
    const finish = (didTimeOut: boolean) => {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
      if (!didTimeOut && opts?.reload) {
        // Restart the UI too: pull the freshly served bundle rather than
        // leaving the old one running against a new process. Bail out before
        // clearing `restarting` — the page is navigating away, and dropping it
        // would re-enable the button for the frame or two until it does.
        window.location.reload();
        return;
      }
      setRestarting(false);
      setTimedOut(didTimeOut);
      opts?.onBack?.();
    };
    timer.current = setInterval(async () => {
      try {
        await api("/api/mobile");
      } catch {
        if (Date.now() - t0 > RESTART_TIMEOUT_MS) finish(true);
        return; // still down — keep polling
      }
      finish(false);
    }, POLL_INTERVAL_MS);
  }, []);

  return { restarting, timedOut, restart };
}
