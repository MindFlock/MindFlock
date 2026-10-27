/** Settings dialog (ports section 21's shell + nav): left nav picks one
 * screen; useUi.dialogTarget preselects a screen (palette/doctor links).
 *
 * Ticketing, PR review and issue handling used to be screens here. They moved
 * to the Intake dialog — they are somewhere you visit to see what came in, not
 * somewhere you configure once — and what is left is genuinely set-and-forget.
 * Their old screen keys still route: `gotoScreen("ticketing" | "repo" |
 * "issues")` opens Intake on the matching tab instead of a blank pane, which
 * matters because the server hands those keys back on Connections cards.
 *
 * Trusting another machine has two ways and each control one screen (the
 * "Devices and people" group): Your devices (key "devices") holds pairing,
 * the fleet key, Make reachable, the shared phone link, settings sync and
 * an Advanced fold with the older routes; Your phone ("mobile") is the QR
 * and /m link; Voice ("voice") is speech for the desktop mic and the phone
 * page; People ("peer") is peer links; Security keeps the access
 * token and the gate. Keys never changed — screenKeys.ts maps the new
 * names and the moved controls' names onto them.
 *
 * Basic vs advanced (settingsMode.tsx): the nav lists the seven screens a new
 * user needs — General, Connections, Notifications, Agents, Workspace,
 * Appearance, Voice — until "Show advanced settings" at its foot reveals the rest
 * (a synced pref). Screens marked `advanced` below are the rest; a deep link
 * to one still opens it, listed last under an "Advanced" heading.
 *
 * The nav opens with "Search settings…" (SettingsSearch.tsx, the palette's
 * index). Below 600 px the screen buttons give way to one <select>: a wrap
 * of tabs re-flowed whenever the bold active tab changed width.
 *
 * Every save marks the control it came from with "✓ Saved" (savedCue.ts):
 * the screens pane notes the last control changed or left, and
 * useSettings' saveField marks it.
 *
 * The Settings tour walks several screens: each of its steps names one
 * (lib/tours/settings.ts `screen`), and while that tour is open the dialog
 * switches to the current step's screen whenever the step changes — Next,
 * Back, or the "?" restarting it on Connections. Clicking another screen
 * mid-step is left alone until the next step.
 *
 * Labels follow the shared glossary ("Agents", "Custom agents"); the keys
 * ("coding", "providers") never change — palette/settingsSearch.ts, deep
 * links and the server's Connections rows use them. */

import { DEFAULT_SETTINGS_SCREEN } from "../../lib/openSettings";
import { openingScreen, rememberSettingsScreen } from "../../lib/settingsLastScreen";
import { Fragment, useEffect, useMemo, useState } from "react";
import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { SettingsCtx, useSettingsModel } from "./useSettings";
import { LEGACY_SCREEN_TABS } from "../intake/IntakeDialog";
import { resolveScreenKey } from "./screenKeys";
import {
  AdvancedToggle,
  navHeading,
  navScreens,
  readAdvancedMode,
  SettingsModeCtx,
  writeAdvancedMode,
} from "./settingsMode";
import { isDevShell } from "../../lib/shell";
import { SettingsSearch } from "./SettingsSearch";
import { noteTouched } from "./savedCue";
import { General } from "./screens/General";
import { Appearance } from "./screens/Appearance";
import { Mobile } from "./screens/Mobile";
import { Voice } from "./screens/Voice";
import { PeerLinks } from "./screens/PeerLinks";
import { Connections } from "./screens/Connections";
import { Notifications } from "./screens/Notifications";
import { CodingCli } from "./screens/CodingCli";
import { Accounts } from "./screens/Accounts";
import { LocalModel } from "./screens/LocalModel";
import { AgentOrchestration } from "./screens/AgentOrchestration";
import { Workspace } from "./screens/Workspace";
import { Ide } from "./screens/Ide";
import { Providers } from "./screens/Providers";
import { Security } from "./screens/Security";
import { Devices } from "./screens/Devices";
import { Doctor } from "./screens/Doctor";
import { SystemLogs } from "./screens/SystemLogs";
import { Advanced } from "./screens/Advanced";
import { Extensions } from "./screens/Extensions";
import { Traffic } from "./screens/Traffic";
import { TourButton } from "../onboarding/TourButton";
import { useAutoTour } from "../onboarding/useAutoTour";
import { TOURS } from "../../lib/tours";

export interface ScreenProps {
  active: boolean;
  /** Navigate within Settings — or, for a retired screen key, hand off to the
   * Intake dialog on the tab that replaced it. */
  gotoScreen(name: string): void;
  onOpenSysLogsPane(): void;
}

/** The nav, in reading order. `group` names the heading a run of screens sits
 * under: the everyday three lead ungrouped, then what configures the agents,
 * the code they work on, this machine, and the troubleshooting tail. Purely a
 * heading — keys and labels are what deep links and the palette resolve, so
 * they never move with it. (Kept AFTER `label` so `{ key, label` stays one
 * contiguous phrase for whoever greps for a screen.)
 *
 * `advanced` hides the screen from the basic nav (settingsMode.tsx): the
 * basic set is what "point MindFlock at my repo and tracker and run agents"
 * needs, everything else waits behind "Show advanced settings". */
interface ScreenDef {
  key: string;
  label: string;
  group?: string;
  advanced?: boolean;
  el: (p: ScreenProps) => React.ReactNode;
}

const SCREENS = [
  { key: "general", label: "General", el: (p) => <General {...p} /> },
  { key: "connections", label: "Connections", el: (p) => <Connections {...p} /> },
  { key: "notifications", label: "Notifications", el: (p) => <Notifications {...p} /> },
  { key: "coding", label: "Agents", group: "Agents", el: (p) => <CodingCli {...p} /> },
  { key: "providers", label: "Custom agents", group: "Agents", advanced: true, el: (p) => <Providers {...p} /> },
  { key: "accounts", label: "Accounts", group: "Agents", advanced: true, el: (p) => <Accounts {...p} /> },
  { key: "localmodel", label: "Local model", group: "Agents", advanced: true, el: (p) => <LocalModel {...p} /> },
  { key: "orchestration", label: "Agent orchestration", group: "Agents", advanced: true, el: (p) => <AgentOrchestration {...p} /> },
  { key: "workspace", label: "Workspace", group: "Code", el: (p) => <Workspace {...p} /> },
  { key: "ide", label: "IDE", group: "Code", advanced: true, el: (p) => <Ide {...p} /> },
  { key: "security", label: "Security", group: "This device", advanced: true, el: (p) => <Security {...p} /> },
  { key: "appearance", label: "Appearance", group: "This device", el: (p) => <Appearance {...p} /> },
  // Other machines and other people. Keys stay "devices" / "mobile" / "peer"
  // (old deep links, the palette, docs) — screenKeys.ts maps the new names.
  { key: "devices", label: "Your devices", group: "Devices and people", advanced: true, el: (p) => <Devices {...p} /> },
  { key: "mobile", label: "Your phone", group: "Devices and people", advanced: true, el: (p) => <Mobile {...p} /> },
  // Voice is basic: the desktop mic and the phone page both use it, and a
  // fresh install's built-in engine needs a newcomer to find "Try it".
  { key: "voice", label: "Voice", group: "Devices and people", el: (p) => <Voice {...p} /> },
  { key: "peer", label: "People", group: "Devices and people", advanced: true, el: (p) => <PeerLinks {...p} /> },
  // Extensions are a thing you add, not a fault to chase: their own group.
  { key: "extensions", label: "Extensions", group: "Extensions", advanced: true, el: (p) => <Extensions {...p} /> },
  { key: "doctor", label: "Doctor", group: "Troubleshooting", advanced: true, el: (p) => <Doctor {...p} /> },
  { key: "logs", label: "System logs", group: "Troubleshooting", advanced: true, el: (p) => <SystemLogs {...p} /> },
  { key: "advanced", label: "Engine & updates", group: "Troubleshooting", advanced: true, el: (p) => <Advanced {...p} /> },
  // Maintainer-only: MindFlock's own reach (stars, downloads, tracked-link
  // clicks), not something an end user's build needs — filtered out below
  // unless this is a --mindflock-dev shell.
  { key: "traffic", label: "Site traffic", group: "Troubleshooting", advanced: true, el: (p) => <Traffic {...p} /> },
] as const satisfies readonly ScreenDef[];

/** A Settings screen key, derived from SCREENS: open one with
 * lib/openSettings, where a typo'd key fails tsc. */
export type SettingsScreen = (typeof SCREENS)[number]["key"];

export function SettingsDialog({ onOpenSysLogsPane }: { onOpenSysLogsPane?: () => void }) {
  const open = useUi((s) => s.openDialog === "settings");
  useAutoTour("settings", open);
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const model = useSettingsModel(open);
  const openDialogFor = useUi((s) => s.openDialogFor);
  // Site traffic is MindFlock's own maintainer dashboard — hidden from nav,
  // deep-link and fallback resolution alike unless this is a dev-shell build.
  const screens = useMemo(
    (): readonly ScreenDef[] => SCREENS.filter((s) => s.key !== "traffic" || isDevShell()),
    []
  );

  // A retired screen key hands off to Intake rather than selecting nothing (which
  // rendered a blank right-hand pane, since .set-screen.active would match no
  // section). Unknown keys still fall back to General.
  const keys = useMemo(() => screens.map((s) => s.key), [screens]);
  // Seeded from the deep link too (not only the effect below), so the first
  // paint is already the linked screen rather than a flash of General.
  const [screen, setScreenState] = useState(() => openingScreen(target, keys));
  // Where you were is where you come back to (until the page reloads).
  const setScreen = (key: string) => {
    rememberSettingsScreen(key);
    setScreenState(key);
  };
  const [advanced, setAdvanced] = useState(readAdvancedMode);
  const changeMode = (on: boolean) => {
    setAdvanced(on);
    writeAdvancedMode(on);
  };
  const gotoScreen = (name: string) => {
    const tab = LEGACY_SCREEN_TABS[name];
    if (tab) openDialogFor("intake", tab);
    else setScreen(resolveScreenKey(name, keys) || DEFAULT_SETTINGS_SCREEN);
  };

  useEffect(() => {
    if (!open) return;
    const tab = target ? LEGACY_SCREEN_TABS[target] : undefined;
    if (tab) {
      // Someone deep-linked a screen that now lives in Intake — send them there.
      openDialogFor("intake", tab);
      return;
    }
    setScreenState(openingScreen(target, keys));
    // Re-read on every open: a sync may have brought another device's choice.
    setAdvanced(readAdvancedMode());
  }, [open, target, openDialogFor, keys]);

  // The Settings tour's step names the screen it shows (see the header).
  const tourStep = useUi((s) => (s.coachTour?.id === "settings" ? s.coachTour.step : -1));
  useEffect(() => {
    if (!open || tourStep < 0) return;
    const want = TOURS.settings.steps[tourStep]?.screen;
    const key = want ? resolveScreenKey(want, keys) : null;
    if (key) setScreen(key);
  }, [open, tourStep, keys]);

  if (!open) return null;

  const shown = navScreens(screens, advanced, screen);
  const hiddenCount = screens.length - navScreens(screens, false, "").length;

  const props: ScreenProps = {
    active: true,
    gotoScreen,
    onOpenSysLogsPane: () => {
      onOpenSysLogsPane?.();
      closeDialog();
    },
  };

  return (
    <SettingsCtx.Provider value={model}>
      <SettingsModeCtx.Provider value={advanced}>
      <Modal id="settings-dialog" onClose={closeDialog}>
        <div id="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-title">
          <div className="ws-head">
            <h2 id="settings-title">Settings</h2>
            <TourButton tour="settings" />
            <button type="button" id="settings-close" onClick={closeDialog}>
              Close
            </button>
          </div>
          <div id="settings-body">
            <nav
              id="settings-nav"
              aria-label="Settings sections"
              data-mode={advanced ? "advanced" : "basic"}
            >
              <SettingsSearch
                onPick={(key, adv) => {
                  // An advanced-only control renders nothing in basic mode.
                  if (adv && !advanced) changeMode(true);
                  gotoScreen(key);
                }}
              />
              {/* Phone width: one select instead of a wrap of tabs. Same list
                  as the buttons (CSS shows one or the other). */}
              <select
                id="settings-nav-select"
                className="set-nav-select"
                aria-label="Settings screen"
                value={screen}
                onChange={(e) => setScreen(e.target.value)}
              >
                {shown.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.label}
                  </option>
                ))}
              </select>
              {shown.map((s, i) => {
                // A heading before each group's first screen (advanced mode),
                // or "Advanced" over a deep-linked advanced screen (basic) —
                // text, not a control, so it takes no Tab stop and no click.
                const heading = navHeading(shown, i, advanced);
                return (
                  <Fragment key={s.key}>
                    {heading ? (
                      <div className="set-nav-group" role="presentation">
                        {heading}
                      </div>
                    ) : null}
                    <button
                      type="button"
                      className={"set-nav-item" + (screen === s.key ? " active" : "")}
                      data-screen={s.key}
                      data-advanced={s.advanced ? "" : undefined}
                      onClick={() => setScreen(s.key)}
                    >
                      {s.label}
                    </button>
                  </Fragment>
                );
              })}
              <AdvancedToggle advanced={advanced} hidden={hiddenCount} onChange={changeMode} />
            </nav>
            <div
              id="settings-screens"
              onChangeCapture={(e) => noteTouched(e.target)}
              onBlurCapture={(e) => noteTouched(e.target)}
            >
              {screens.map((s) => (
                <section
                  key={s.key}
                  className={"set-screen" + (screen === s.key ? " active" : "")}
                  data-screen={s.key}
                  data-caps-need={s.key === "workspace" ? "git" : undefined}
                >
                  {screen === s.key && s.el(props)}
                </section>
              ))}
            </div>
          </div>
        </div>
      </Modal>
      </SettingsModeCtx.Provider>
    </SettingsCtx.Provider>
  );
}
