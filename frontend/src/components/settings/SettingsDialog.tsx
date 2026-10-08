/** Settings dialog (ports section 21's shell + nav): left nav picks one
 * screen; useUi.dialogTarget preselects a screen (palette/doctor links).
 *
 * Ticketing, PR review and issue handling used to be screens here. They moved
 * to the Intake dialog — they are somewhere you visit to see what came in, not
 * somewhere you configure once — and what is left is genuinely set-and-forget.
 * Their old screen keys still route: `gotoScreen("ticketing" | "repo" |
 * "issues")` opens Intake on the matching tab instead of a blank pane, which
 * matters because the server hands those keys back on Connections cards. */

import { Fragment, useEffect, useMemo, useState } from "react";
import { useUi } from "../../state/store";
import { SettingsCtx, useSettingsModel } from "./useSettings";
import { LEGACY_SCREEN_TABS } from "../intake/IntakeDialog";
import { isDevShell } from "../../lib/shell";
import { General } from "./screens/General";
import { Appearance } from "./screens/Appearance";
import { Mobile } from "./screens/Mobile";
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
 * contiguous phrase for whoever greps for a screen.) */
const SCREENS: Array<{
  key: string;
  label: string;
  group?: string;
  el: (p: ScreenProps) => React.ReactNode;
}> = [
  { key: "general", label: "General", el: (p) => <General {...p} /> },
  { key: "connections", label: "Connections", el: (p) => <Connections {...p} /> },
  { key: "notifications", label: "Notifications", el: (p) => <Notifications {...p} /> },
  { key: "coding", label: "Agent CLI", group: "Agents", el: (p) => <CodingCli {...p} /> },
  { key: "providers", label: "Agent providers", group: "Agents", el: (p) => <Providers {...p} /> },
  { key: "accounts", label: "Accounts", group: "Agents", el: (p) => <Accounts {...p} /> },
  { key: "localmodel", label: "Local model", group: "Agents", el: (p) => <LocalModel {...p} /> },
  { key: "orchestration", label: "Agent orchestration", group: "Agents", el: (p) => <AgentOrchestration {...p} /> },
  { key: "workspace", label: "Workspace", group: "Code", el: (p) => <Workspace {...p} /> },
  { key: "ide", label: "IDE", group: "Code", el: (p) => <Ide {...p} /> },
  { key: "devices", label: "Devices", group: "This device", el: (p) => <Devices {...p} /> },
  { key: "security", label: "Security", group: "This device", el: (p) => <Security {...p} /> },
  { key: "appearance", label: "Appearance", group: "This device", el: (p) => <Appearance {...p} /> },
  { key: "mobile", label: "Mobile", group: "This device", el: (p) => <Mobile {...p} /> },
  { key: "peer", label: "Peer links", group: "This device", el: (p) => <PeerLinks {...p} /> },
  { key: "doctor", label: "Doctor", group: "Troubleshooting", el: (p) => <Doctor {...p} /> },
  { key: "logs", label: "System logs", group: "Troubleshooting", el: (p) => <SystemLogs {...p} /> },
  { key: "advanced", label: "Advanced", group: "Troubleshooting", el: (p) => <Advanced {...p} /> },
  { key: "extensions", label: "Extensions", group: "Troubleshooting", el: (p) => <Extensions {...p} /> },
  // Maintainer-only: MindFlock's own reach (stars, downloads, tracked-link
  // clicks), not something an end user's build needs — filtered out below
  // unless this is a --mindflock-dev shell.
  { key: "traffic", label: "Site traffic", group: "Troubleshooting", el: (p) => <Traffic {...p} /> },
];

export function SettingsDialog({ onOpenSysLogsPane }: { onOpenSysLogsPane?: () => void }) {
  const open = useUi((s) => s.openDialog === "settings");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [screen, setScreen] = useState("general");
  const model = useSettingsModel(open);
  const openDialogFor = useUi((s) => s.openDialogFor);
  // Site traffic is MindFlock's own maintainer dashboard — hidden from nav,
  // deep-link and fallback resolution alike unless this is a dev-shell build.
  const screens = useMemo(() => SCREENS.filter((s) => s.key !== "traffic" || isDevShell()), []);

  // A retired screen key hands off to Intake rather than selecting nothing (which
  // rendered a blank right-hand pane, since .set-screen.active would match no
  // section). Unknown keys still fall back to General.
  const gotoScreen = (name: string) => {
    const tab = LEGACY_SCREEN_TABS[name];
    if (tab) openDialogFor("intake", tab);
    else setScreen(screens.some((s) => s.key === name) ? name : "general");
  };

  useEffect(() => {
    if (!open) return;
    const tab = target ? LEGACY_SCREEN_TABS[target] : undefined;
    if (tab) {
      // Someone deep-linked a screen that now lives in Intake — send them there.
      openDialogFor("intake", tab);
      return;
    }
    setScreen(target && screens.some((s) => s.key === target) ? target : "general");
  }, [open, target, openDialogFor, screens]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeDialog();
        e.preventDefault();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!open) return null;

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
      <div
        id="settings-dialog"
        className="modal"
        onClick={(e) => {
          if (e.target === e.currentTarget) closeDialog();
        }}
      >
        <div id="settings-panel">
          <div className="ws-head">
            <h2>Settings</h2>
            <button type="button" id="settings-close" onClick={closeDialog}>
              Close
            </button>
          </div>
          <div id="settings-body">
            <nav id="settings-nav" aria-label="Settings sections">
              {screens.map((s, i) => (
                <Fragment key={s.key}>
                  {/* A heading before each group's first screen — text, not a
                      control, so it takes no Tab stop and no click. */}
                  {s.group && s.group !== screens[i - 1]?.group ? (
                    <div className="set-nav-group" role="presentation">
                      {s.group}
                    </div>
                  ) : null}
                  <button
                    type="button"
                    className={"set-nav-item" + (screen === s.key ? " active" : "")}
                    data-screen={s.key}
                    onClick={() => setScreen(s.key)}
                  >
                    {s.label}
                  </button>
                </Fragment>
              ))}
            </nav>
            <div id="settings-screens">
              {screens.map((s) => (
                <section
                  key={s.key}
                  className={"set-screen" + (screen === s.key ? " active" : "")}
                  data-screen={s.key}
                  data-caps-need={
                    s.key === "mobile" ? "tailscale" : s.key === "workspace" ? "git" : undefined
                  }
                >
                  {screen === s.key && s.el(props)}
                </section>
              ))}
            </div>
          </div>
        </div>
      </div>
    </SettingsCtx.Provider>
  );
}
