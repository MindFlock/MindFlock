/**
 * Settings → Your devices → "Reach my devices from anywhere (no Tailscale
 * needed)" — ONE switch and the plain status line under it
 * (docs/devices-anywhere.md). Off by default. The line is the server's:
 * "On · reachable through a private tunnel", "Couldn't start the tunnel —
 * <why> · Retry", "GitHub sign-in is missing the repo permission — …".
 * When cloudflared is missing, the doctor's one-click install (the same one
 * People and guest links use) is offered right here.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import {
  ANYWHERE_HINT,
  ANYWHERE_LABEL,
  anywhereLine,
  anywherePollMs,
  anywhereWarn,
  type AnywhereStatus,
} from "../../../lib/devicesAnywhere";
import { toast } from "../../../lib/toast";
import { reprobeDoctor } from "../../../state/queries";
import { InstallMissing, type InstallStep } from "../../dialogs/InstallTerminal";

const PATH = "/api/devices/anywhere";

export function DevicesAnywhere({ active }: { active: boolean }) {
  const [st, setSt] = useState<AnywhereStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [install, setInstall] = useState<InstallStep[]>([]);

  const load = useCallback(async () => {
    try {
      setSt(await api<AnywhereStatus>(PATH));
    } catch {
      /* an older server: no row */
    }
  }, []);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  const ms = anywherePollMs(st);
  useEffect(() => {
    if (!active || !st?.on) return;
    const t = setInterval(() => {
      if (!document.hidden) void load();
    }, ms);
    return () => clearInterval(t);
  }, [active, st?.on, ms, load]);

  const needsInstall = !!st?.on && st.install;
  useEffect(() => {
    if (!needsInstall) {
      setInstall([]);
      return;
    }
    let live = true;
    reprobeDoctor()
      .then((d) => live && setInstall(d.install?.steps || []))
      .catch(() => live && setInstall([]));
    return () => {
      live = false;
    };
  }, [needsInstall]);

  const post = async (path: string, body?: object) => {
    setBusy(true);
    try {
      setSt(await api<AnywhereStatus>(path, { json: body ?? {} }));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!st) return null;
  const line = anywhereLine(st);
  return (
    <div className="devices-this" id="devices-anywhere">
      <div
        className="set-row set-switch-row"
        id="devices-anywhere-row"
        data-control="devices-anywhere"
        title={ANYWHERE_HINT}
      >
        <span className="set-label">{ANYWHERE_LABEL}</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            id="devices-anywhere-switch"
            data-group="general"
            data-field="devices_anywhere"
            checked={st.on}
            disabled={busy}
            onChange={(e) => void post(PATH, { on: e.target.checked })}
          />
          <span className="ca-slider" />
        </label>
        <span className="set-hint">{ANYWHERE_HINT}</span>
      </div>
      {st.on && line && (
        <p
          className={anywhereWarn(st) ? "devices-note warn" : "devices-note"}
          id="devices-anywhere-status"
          role={anywhereWarn(st) ? "alert" : undefined}
        >
          {line}
          {st.retry && (
            <>
              {" "}
              <button
                type="button"
                className="test-btn"
                id="devices-anywhere-retry"
                disabled={busy}
                onClick={() => void post(PATH + "/retry")}
              >
                Retry
              </button>
            </>
          )}
        </p>
      )}
      {needsInstall && <InstallMissing steps={install} onDone={() => void load()} />}
    </div>
  );
}
