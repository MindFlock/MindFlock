/** What the surrounding desktop shell is, for the few places the UI has to
 * match a platform convention rather than pick its own.
 *
 * Only the Electron shell counts. A Mac user viewing the web UI in Safari has a
 * browser chrome of its own and no traffic lights to make room for, so the
 * layout must not shift for them — hence every check here goes through the
 * preload bridge, never a Mac-looking user agent.
 */

interface ShellBridge {
  dev?: boolean;
  platform?: string;
  /** The OS draws this window's controls, top-left (macOS traffic lights). */
  nativeTitleBar?: boolean;
}

interface WinCtl {
  isFullScreen?: () => Promise<boolean>;
  onFullScreenChanged?: (cb: (isFull: boolean) => void) => (() => void) | void;
}

function winctl(): WinCtl | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as unknown as { winctl?: WinCtl }).winctl;
}

/** Whether the shell window is fullscreen right now; false in a browser or on a
 * shell build without the query. The transition event only fires on a CHANGE, so
 * a window that is already fullscreen when the UI loads needs this. */
export async function isFullScreen(): Promise<boolean> {
  try {
    return (await winctl()?.isFullScreen?.()) === true;
  } catch {
    return false; // an IPC that rejects means "assume windowed", never a crash
  }
}

/** The preload bridge, or undefined — including when there is no `window` at
 * all, so these are safe to call from any context. */
function bridge(): ShellBridge | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as unknown as { mfshell?: ShellBridge }).mfshell;
}

/** True inside the desktop shell (any OS); false in a plain browser. */
export function inShell(): boolean {
  return !!bridge();
}

/** True inside the desktop shell on macOS. Prefer
 * {@link hasNativeWindowControls} for layout decisions — see its note. */
export function isMacShell(): boolean {
  return bridge()?.platform === "darwin";
}

/**
 * True when the window's controls are drawn by the OS in the TOP-LEFT corner,
 * so the top bar must leave room for them and mirror its own cluster right.
 *
 * Deliberately the shell's own capability flag rather than
 * `platform === "darwin"`: the frontend ships inside the engine and the shell
 * ships as the desktop app, on separate release cadences. A shell built before
 * this change still injects its own controls top-right on a Mac, and a
 * platform-keyed layout would stack the logo/theme/bell on top of them. The
 * flag is absent there, so that build keeps the layout it was built for.
 */
export function hasNativeWindowControls(): boolean {
  return bridge()?.nativeTitleBar === true;
}

/** Subscribe to shell fullscreen changes; returns an unsubscribe (no-op in a
 * browser, or on a shell build without the bridge method). macOS hides the
 * traffic lights in fullscreen, so the reserved gap has to collapse. */
export function onFullScreenChanged(cb: (isFull: boolean) => void): () => void {
  const off = winctl()?.onFullScreenChanged?.(cb);
  return typeof off === "function" ? off : () => {};
}
