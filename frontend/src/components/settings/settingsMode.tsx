/** Basic vs advanced Settings.
 *
 * A new user came to point MindFlock at a repo and a tracker and run agents;
 * the other screens (other machines, the phone, people, accounts, providers'
 * internals, orchestration, extensions, troubleshooting) are real features
 * but noise on day one. So the nav shows the basic screens until the person
 * flips "Show advanced settings" at its foot, and the advanced controls that
 * live ON a basic screen sit in an <AdvancedFold> there, which basic mode
 * does not render at all.
 *
 * The choice is a synced pref (`prefs.settings_advanced`, localStorage
 * `mf_settings_advanced` as the cache — lib/prefs.ts), so it follows the
 * person to their other devices like the theme does; it is never a
 * per-browser first-run flag.
 *
 * A deep link (palette, a Connections card, gotoScreen, lib/openSettings)
 * to an advanced screen still lands on it in basic mode: the active screen
 * is always in the nav — at the END, under an "Advanced" heading — without
 * flipping the saved mode. At the end, because in its usual place the
 * heading sat above it and every basic screen after it (Workspace and
 * Appearance read as advanced ones). */

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { lsKeyFor, notePrefWrite, readLocal, writeLocal, type KV } from "../../lib/prefs";
import "./settingsMode.css";

/** The localStorage key the pref is cached under. */
export const ADVANCED_LS = lsKeyFor("settings_advanced");

/** Whether this person chose to see advanced settings (default: no). */
export function readAdvancedMode(kv?: KV | null): boolean {
  return readLocal("settings_advanced", kv) === true;
}

/** Save the choice locally and queue it for the server (and so every one of
 * the person's devices). */
export function writeAdvancedMode(on: boolean, kv?: KV | null): void {
  writeLocal("settings_advanced", on, kv);
  notePrefWrite(ADVANCED_LS, kv);
}

/** The slice of a screen definition this module needs. */
export interface ModeScreen {
  key: string;
  group?: string;
  advanced?: boolean;
}

/** The screens the nav lists: every screen in advanced mode; in basic mode
 * the basic ones, in order, then the active screen when it is advanced (a
 * deep link landed there — the nav must show where you are) last, so its
 * "Advanced" heading never sits over a basic screen. */
export function navScreens<T extends ModeScreen>(screens: readonly T[], advanced: boolean, active: string): T[] {
  if (advanced) return [...screens];
  const basic = screens.filter((s) => !s.advanced);
  const peek = screens.find((s) => s.advanced && s.key === active);
  return peek ? [...basic, peek] : basic;
}

/** The heading to draw before nav item `i` of `shown`, or "" for none. In
 * advanced mode that is the group heading at each group's first screen. In
 * basic mode the few screens left read as one short list with no headings
 * (a heading over a single item is noise), except "Advanced" over a peeked
 * advanced screen, so it does not pass for a basic one. */
export function navHeading(shown: readonly ModeScreen[], i: number, advanced: boolean): string {
  const s = shown[i];
  if (!advanced) return s.advanced && !shown[i - 1]?.advanced ? "Advanced" : "";
  return s.group && s.group !== shown[i - 1]?.group ? s.group : "";
}

/** Whether the person is in advanced mode — read by the folds on basic
 * screens (open by default in advanced mode, closed in basic). */
export const SettingsModeCtx = createContext<boolean>(false);

export function useAdvancedSettings(): boolean {
  return useContext(SettingsModeCtx);
}

/** The foot-of-nav switch. */
export function AdvancedToggle({
  advanced,
  hidden,
  onChange,
}: {
  advanced: boolean;
  /** How many screens basic mode leaves out. */
  hidden: number;
  onChange(on: boolean): void;
}) {
  return (
    <div className="set-mode-foot">
      <button
        type="button"
        id="settings-advanced-toggle"
        className="set-mode-toggle"
        aria-pressed={advanced}
        onClick={() => onChange(!advanced)}
      >
        {advanced ? "Hide advanced settings" : "Show advanced settings"}
      </button>
      {!advanced && hidden > 0 ? <span className="set-mode-more">{hidden} more screens</span> : null}
    </div>
  );
}

/** An "Advanced" section on a basic screen: power-user rows that only exist
 * in advanced mode. Basic mode renders NOTHING for it — not a closed fold —
 * so a newcomer never meets "Merge when green" or pre-commit hook ids one
 * click away; there is one advanced mechanism (the nav's "Show advanced
 * settings"), not two. In advanced mode it is an open fold the person can
 * collapse.
 *
 * `basic` keeps it on basic screens as a CLOSED fold instead — for a part
 * that is optional rather than expert (Notifications' phone alerts and its
 * longer alert list). */
export function AdvancedFold({
  id,
  summary = "Advanced",
  basic = false,
  defaultOpen,
  children,
}: {
  id: string;
  summary?: string;
  /** Show it (folded) in basic mode too. */
  basic?: boolean;
  /** Whether it starts open; default: open in advanced mode. `false` keeps a
   * long, rarely-needed section (Connections' "More connections") folded
   * until asked for, whatever the mode. */
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const advanced = useAdvancedSettings();
  const initial = defaultOpen ?? advanced;
  const [open, setOpen] = useState(initial);
  useEffect(() => setOpen(initial), [initial]);
  if (!advanced && !basic) return null;
  return (
    <details
      className="pr-advanced set-adv-fold"
      id={id}
      data-advanced-fold=""
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>{summary}</summary>
      <div className="pr-advanced-body set-adv-fold-body">{children}</div>
    </details>
  );
}
