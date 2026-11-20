/** Settings → Advanced's Updates block, the desktop shell's half
 * (electron/main.js `engine:update-info`): whether the shell runs from source
 * (it never installs then; lib/updates installPath), and the words for the
 * shell's own automatic-updates switch, which is drawn only when the server
 * is too old to hold the setting itself. Pure, so vitest pins them. */

/** What `mfengine.updateInfo()` adds beyond the engine fields. Older shells
 * send neither key. */
export type ShellUpdateFlags = {
  /** The shell runs from source (dev shell / unpackaged `electron .`). */
  source?: boolean;
  /** Automatic updates on (absent: a shell from before the switch existed). */
  autoUpdate?: boolean;
};

/** "source": a dev run of the shell — no app updates to offer, so the server
 * installs. "shell": the desktop shell's own updater. */
export function shellUpdateMode(info: ShellUpdateFlags | null | undefined): "source" | "shell" {
  return info?.source ? "source" : "shell";
}

/** The switch only exists on shells that report it — never on a source run. */
export function showAutoUpdateSwitch(info: ShellUpdateFlags | null | undefined): boolean {
  return !info?.source && typeof info?.autoUpdate === "boolean";
}

export function autoUpdateHint(on: boolean): string {
  return on
    ? "New releases download in the background and install themselves once you’ve been away " +
        "for 10 minutes (or the screen is locked) and no agent is mid-turn — waiting at most " +
        "2 hours for that. The app restarts onto the new version and says what it updated. " +
        "“Skip this version” is still honoured."
    : "Updates are only offered: the toast’s Update button (or the one below) installs them " +
        "when you choose.";
}
