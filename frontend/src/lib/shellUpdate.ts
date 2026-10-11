/** Settings → Advanced's desktop-shell update section (electron/main.js
 * `engine:update-info`): which view a shell's answer calls for, and the words
 * for the automatic-updates switch. Pure, so vitest pins them. */

/** What `mfengine.updateInfo()` adds beyond the engine fields. Older shells
 * send neither key. */
export type ShellUpdateFlags = {
  /** The shell runs from source (dev shell / unpackaged `electron .`). */
  source?: boolean;
  /** Automatic updates on (absent: a shell from before the switch existed). */
  autoUpdate?: boolean;
};

/** "source": a git checkout — no app updates to offer, so the engine's own
 * self-update view (which walks an editable checkout through git) is shown
 * with a one-line note. "shell": the desktop shell's own updater. */
export function shellUpdateMode(info: ShellUpdateFlags | null | undefined): "source" | "shell" {
  return info?.source ? "source" : "shell";
}

/** The switch only exists on shells that report it — never on a source run. */
export function showAutoUpdateSwitch(info: ShellUpdateFlags | null | undefined): boolean {
  return !info?.source && typeof info?.autoUpdate === "boolean";
}

export const SOURCE_RUN_NOTE =
  "This desktop app is running from source (a dev run), so it never updates itself — " +
  "update the checkout with git. Nothing here offers or installs a release.";

export function autoUpdateHint(on: boolean): string {
  return on
    ? "New releases download in the background and install themselves once you’ve been away " +
        "for 10 minutes (or the screen is locked) and no agent is mid-turn — waiting at most " +
        "2 hours for that. The app restarts onto the new version and says what it updated. " +
        "“Skip this version” is still honoured."
    : "Updates are only offered: the toast’s Update button (or the one below) installs them " +
        "when you choose.";
}
