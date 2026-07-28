/** Settings dialog (ports section 21's shell + nav): left nav picks one
 * screen; useUi.dialogTarget preselects a screen (palette/doctor links). */

import { useEffect, useState } from "react";
import { useUi } from "../../state/store";
import { prefetchSettingsPanels } from "../../state/queries";
import { SettingsCtx, useSettingsModel } from "./useSettings";
import { General } from "./screens/General";
import { Appearance } from "./screens/Appearance";
import { Mobile } from "./screens/Mobile";
import { Connections } from "./screens/Connections";
import { Notifications } from "./screens/Notifications";
import { CodingCli } from "./screens/CodingCli";
import { Ticketing } from "./screens/Ticketing";
import { Workspace } from "./screens/Workspace";
import { PrReview } from "./screens/PrReview";
import { GitIssues } from "./screens/GitIssues";
import { Ide } from "./screens/Ide";
import { Providers } from "./screens/Providers";
import { Security } from "./screens/Security";
import { Doctor } from "./screens/Doctor";
import { SystemLogs } from "./screens/SystemLogs";
import { Advanced } from "./screens/Advanced";

export interface ScreenProps {
  active: boolean;
  gotoScreen(name: string): void;
  onOpenSysLogsPane(): void;
}

const SCREENS: Array<{ key: string; label: string; el: (p: ScreenProps) => React.ReactNode }> = [
  { key: "general", label: "General", el: (p) => <General {...p} /> },
  { key: "connections", label: "Connections", el: (p) => <Connections {...p} /> },
  { key: "notifications", label: "Notifications", el: (p) => <Notifications {...p} /> },
  { key: "coding", label: "Agent CLI", el: (p) => <CodingCli {...p} /> },
  { key: "ticketing", label: "Ticketing", el: (p) => <Ticketing {...p} /> },
  { key: "workspace", label: "Workspace", el: (p) => <Workspace {...p} /> },
  { key: "repo", label: "PR review", el: (p) => <PrReview {...p} /> },
  { key: "issues", label: "Git issues", el: (p) => <GitIssues {...p} /> },
  { key: "ide", label: "IDE", el: (p) => <Ide {...p} /> },
  { key: "providers", label: "Agent providers", el: (p) => <Providers {...p} /> },
  { key: "security", label: "Security", el: (p) => <Security {...p} /> },
  { key: "appearance", label: "Appearance", el: (p) => <Appearance {...p} /> },
  { key: "mobile", label: "Mobile", el: (p) => <Mobile {...p} /> },
  { key: "doctor", label: "Doctor", el: (p) => <Doctor {...p} /> },
  { key: "logs", label: "System logs", el: (p) => <SystemLogs {...p} /> },
  { key: "advanced", label: "Advanced", el: (p) => <Advanced {...p} /> },
];

export function SettingsDialog({ onOpenSysLogsPane }: { onOpenSysLogsPane?: () => void }) {
  const open = useUi((s) => s.openDialog === "settings");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [screen, setScreen] = useState("general");
  const model = useSettingsModel(open);

  useEffect(() => {
    if (open) setScreen(target && SCREENS.some((s) => s.key === target) ? target : "general");
  }, [open, target]);

  // Warm the slow panels (tickets / PRs / issues) while the first screen is
  // being read, so clicking through to one doesn't start with a spinner.
  useEffect(() => {
    if (open) prefetchSettingsPanels();
  }, [open]);

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
    gotoScreen: setScreen,
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
              {SCREENS.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  className={"set-nav-item" + (screen === s.key ? " active" : "")}
                  data-screen={s.key}
                  onClick={() => setScreen(s.key)}
                >
                  {s.label}
                </button>
              ))}
            </nav>
            <div id="settings-screens">
              {SCREENS.map((s) => (
                <section
                  key={s.key}
                  className={"set-screen" + (screen === s.key ? " active" : "")}
                  data-screen={s.key}
                  id={
                    s.key === "repo"
                      ? "pr-review-block"
                      : s.key === "issues"
                        ? "git-issues-block"
                        : undefined
                  }
                  data-caps-need={
                    s.key === "mobile"
                      ? "tailscale"
                      : s.key === "workspace"
                        ? "git"
                        : s.key === "repo" || s.key === "issues"
                          ? "git ticketing"
                          : undefined
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
