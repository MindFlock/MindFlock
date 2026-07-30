/** Settings → Doctor (partial 111): same check list as first-run setup. */

import { useState } from "react";
import { DoctorList } from "../../dialogs/SetupDialog";
import type { ScreenProps } from "../SettingsDialog";

export function Doctor(_: ScreenProps) {
  const [reprobeKey, setReprobeKey] = useState(0);
  return (
    <>
      <h3 className="set-section-title">Dependency doctor</h3>
      <div id="settings-doctor">
        <DoctorList reprobeKey={reprobeKey} />
      </div>
      <div className="set-row">
        <span className="test-row">
          <button
            type="button"
            id="settings-doctor-recheck"
            className="test-btn"
            onClick={() => setReprobeKey((k) => k + 1)}
          >
            Re-check
          </button>
        </span>
        <span className="set-hint">
          Probes git, tmux, the agent CLI, uv and tailscale — plus gh, which is
          optional (pushing uses plain git over your own remote).
        </span>
      </div>
    </>
  );
}
