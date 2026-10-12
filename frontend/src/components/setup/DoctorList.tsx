/** The doctor model and its two renderers, shared by the setup checklist's
 * "Engine and tools" step and Settings → Doctor:
 *   DoctorList       — the check list, with "Install everything missing"
 *   useDoctorWarn    — the sidebar's ⚠ chip state
 *   useDoctorAutoShow — headless: load + 5-min probe; honours `?setup=1`
 *
 * (Moved out of dialogs/SetupDialog.tsx, which re-exports them, so the Setup
 * modal and the checklist it renders can import this without a cycle.) */

import { useEffect, useState } from "react";
import { create } from "zustand";
import type { DoctorCheckItem, DoctorPayload } from "../../api/types";
import { reprobeDoctor, useDoctor } from "../../state/queries";
import { useUi } from "../../state/store";
import { InstallMissing } from "../dialogs/InstallTerminal";
import { AgentSignIn, signInTarget } from "../dialogs/AgentSignIn";
import { consumeSetupIntent } from "./setupModel";

// --- Doctor model -------------------------------------------------------------

// The payload is GET /api/doctor's, cached once in state/queries (useDoctor).
export type { DoctorCheckItem, DoctorPayload };

const DOCTOR_ICON: Record<string, string> = { ok: "✓", info: "ℹ", warn: "!", fail: "✗" };

interface DoctorWarnState {
  failing: boolean;
  dismissed: boolean;
  dismiss(): void;
  _setFailing(f: boolean): void;
}

const useDoctorWarnStore = create<DoctorWarnState>((set) => ({
  failing: false,
  dismissed: false, // per page load; ✕ hides until reload
  dismiss: () => set({ dismissed: true }),
  _setFailing: (failing) => set({ failing }),
}));

/** F8 doctor-warn chip state for the sidebar. */
export function useDoctorWarn() {
  return useDoctorWarnStore();
}

/** Headless doctor probe: on load + every 5 minutes (never on the 4s poll).
 *
 * The probe only feeds the sidebar's ⚠ chip now. It used to pop the Setup
 * modal at a first-run user whenever a required tool was missing and a
 * session existed — a third first-run surface on top of the welcome tour and
 * the grid card. The checklist is one surface now: the card on an empty grid
 * while a required step is to do, the "Setup 3/5" chip after, and this modal
 * when either of those (or the palette, or the doctor chip) opens it.
 *
 * The one auto-open left is the desktop app's hand-off: after a first-launch
 * engine install it opens the app with `?setup=1`, asking for the checklist
 * (tmux and the agent CLI are still missing — the engine install can't do
 * them without a terminal). */
export function useDoctorAutoShow() {
  useEffect(() => {
    if (consumeSetupIntent()) useUi.getState().openDialogFor("setup");
  }, []);
  // Any fresh payload — this probe's, or a re-probe from the doctor list —
  // re-reads the chip. A failed fetch keeps the last answer (an unreachable
  // backend is the conn-banner's job).
  const { data } = useDoctor({ refetchInterval: 300_000 });
  useEffect(() => {
    if (data !== undefined) useDoctorWarnStore.getState()._setFailing(!(data && data.ok));
  }, [data]);
}

/** The doctor check list (setup checklist step 1 + Settings → Doctor).
 * `onChange` fires after an install or a sign-in finished, so the checklist
 * can re-read its steps. */
export function DoctorList({ reprobeKey, onChange }: { reprobeKey?: number; onChange?(): void }) {
  // Bumped when the install window finishes, so the list re-probes itself.
  const [installed, setInstalled] = useState(0);
  const reprobe = (reprobeKey || 0) > 0 || installed > 0;
  // A plain open reads the shared payload (refetched on mount); a re-probe is
  // asked for explicitly below and lands in the same cache.
  const query = useDoctor({ enabled: !reprobe });
  const [reprobeError, setReprobeError] = useState("");

  useEffect(() => {
    if (!reprobe) return;
    let live = true;
    reprobeDoctor().then(
      () => {
        if (!live) return;
        setReprobeError("");
        onChange?.();
      },
      (e) => {
        if (live) setReprobeError((e as Error).message);
      }
    );
    return () => {
      live = false;
    };
    // onChange is a notification, not an input: re-running the probe because
    // the parent re-rendered with a new closure would re-probe forever.
  }, [reprobeKey, installed]);

  const doctor = query.data ?? null;
  const error = reprobe ? reprobeError : query.error ? (query.error as Error).message : "";

  if (error) return <p className="error">doctor failed: {error}</p>;
  if (!doctor) return <p className="muted">Checking dependencies…</p>;
  if (!doctor.checks || !doctor.checks.length) return <p className="muted">doctor unavailable</p>;
  return (
    <>
      <InstallMissing steps={doctor.install?.steps} onDone={() => setInstalled((n) => n + 1)} />
      <ul className="doctor-list">
        {doctor.checks.map((c, i) => (
          <li key={c.id || i} className={"doctor-check st-" + (c.status || "info")}>
            <span className="doctor-ico">{DOCTOR_ICON[c.status || ""] || "•"}</span>
            <span className="doctor-label">{c.label || c.id || ""}</span>
            <span className="doctor-detail">
              {c.detail || ""}
              {c.fix && c.status !== "ok" && <span className="doctor-fix"> fix: {c.fix}</span>}
              {signInTarget(c) && (
                <AgentSignIn
                  provider={signInTarget(c) as string}
                  className="doctor-signin"
                  onDone={() => setInstalled((n) => n + 1)}
                />
              )}
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}
