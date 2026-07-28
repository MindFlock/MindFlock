/** Settings → IDE (partial 113 + section 21's D2 picker + the auto-adopt
 * switch from section 20): detected-IDE select, custom command fallback,
 * and the IDE auto-adopt toggle. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { refreshConfig } from "../../../state/queries";
import { toast } from "../../../lib/toast";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

const CUSTOM_IDE = "__custom__";

interface IdeEntry {
  command: string;
  name: string;
  installed?: boolean;
}

export function Ide(_: ScreenProps) {
  const s = useSettings();
  const [ides, setIdes] = useState<IdeEntry[] | null>(null);
  const [value, setValue] = useState(CUSTOM_IDE);
  const [customVisible, setCustomVisible] = useState(false);
  const [autoAdopt, setAutoAdopt] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const data = await api<{ ides?: IdeEntry[]; current?: string }>("/api/ides");
        const list = data.ides || [];
        setIdes(list);
        const cur = (data.current || "").trim();
        if (cur && list.some((i) => i.command === cur)) {
          setValue(cur);
          setCustomVisible(false);
        } else {
          setValue(CUSTOM_IDE);
          setCustomVisible(true);
        }
      } catch {
        // No registry — fall back to the custom input only.
        setIdes([]);
        setCustomVisible(true);
      }
      try {
        const a = await api<{ enabled?: boolean }>("/api/cursor/autoadopt");
        setAutoAdopt(!!a?.enabled);
      } catch {
        /* leave as-is */
      }
    })();
  }, []);

  const pick = useCallback(
    (v: string) => {
      setValue(v);
      if (v === CUSTOM_IDE) {
        setCustomVisible(true);
        return; // persisted when the custom input itself changes
      }
      setCustomVisible(false);
      s.saveField("platform", "ide_command", v).then(() => refreshConfig());
    },
    [s]
  );

  return (
    <>
      <h3 className="set-section-title">IDE</h3>
      <label className="set-row">
        <span className="set-label">Editor</span>
        <select
          id="ide-select"
          title="Editor used to open workspaces — detected from PATH; pick Custom command… for anything else."
          value={value}
          onChange={(e) => pick(e.target.value)}
        >
          {(ides || []).map((ide) => (
            <option key={ide.command} value={ide.command} disabled={!ide.installed}>
              {ide.name + (ide.installed ? "" : " (not installed)")}
            </option>
          ))}
          <option value={CUSTOM_IDE}>Custom command…</option>
        </select>
        <span className="set-hint">
          Detected editors are selectable; missing ones are grayed out. Window focus and
          auto-adopt work best with VS Code-family editors.
        </span>
      </label>
      <label className={"set-row" + (customVisible ? "" : " hidden")} id="ide-custom-row">
        <span className="set-label">Custom IDE command</span>
        <SettingField group="platform" field="ide_command" placeholder="cursor" />
        <span className="set-hint">
          Editor CLI used to open workspaces — e.g. cursor, code, windsurf, zed.
        </span>
      </label>
      <div
        className="set-row set-switch-row"
        id="ide-auto-row"
        title="Continuously adopt folders into MindFlock as you open them in your IDE"
      >
        <span className="set-label" id="ide-auto-label">IDE auto-adopt</span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            id="cursor-auto"
            checked={autoAdopt}
            onChange={async (e) => {
              const want = e.target.checked;
              setAutoAdopt(want);
              try {
                const r = await api<{ enabled?: boolean }>("/api/cursor/autoadopt", {
                  json: { enabled: want },
                });
                setAutoAdopt(!!r?.enabled);
                toast(r?.enabled ? "Cursor auto-adopt on" : "Cursor auto-adopt off");
              } catch {
                setAutoAdopt(!want); // revert on failure
                toast("Auto-adopt toggle failed");
              }
            }}
          />
          <span className="ca-slider" />
        </label>
      </div>
    </>
  );
}
