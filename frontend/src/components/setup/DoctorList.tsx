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
import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { InstallMissing, type InstallStep } from "../dialogs/InstallTerminal";
import { AgentSignIn, signInTarget } from "../dialogs/AgentSignIn";
import { consumeSetupIntent } from "./setupModel";

// --- Doctor model -------------------------------------------------------------

export interface DoctorCheckItem {
  id?: string;
  label?: string;
  status?: "ok" | "info" | "warn" | "fail" | string;
  detail?: string;
  fix?: string;
  /** The runnable fix (an agent-auth row's declared login command). */
  cmd?: string;
  /** The agent provider an agent row is about (drives "Sign in to …"). */
  provider?: string;
}

export interface DoctorPayload {
  ok: boolean;
  checks?: DoctorCheckItem[];
  /** Everything missing that this machine needs, as one install (see
   * `backend.doctor.install_plan`). */
  install?: { steps?: InstallStep[]; packages?: string[] };
}

const DOCTOR_ICON: Record<string, string> = { ok: "✓", info: "ℹ", warn: "!", fail: "✗" };

/** Cached GET /api/doctor payload (5-min background probe keeps it warm). */
let lastDoctor: DoctorPayload | null = null;

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
    const check = async () => {
      try {
        const d = await api<DoctorPayload>("/api/doctor");
        lastDoctor = d;
        useDoctorWarnStore.getState()._setFailing(!(d && d.ok));
      } catch {
        /* unreachable backend is the conn-banner's job */
      }
    };
    check();
    const t = setInterval(() => {
      if (!document.hidden) check();
    }, 300000);
    return () => clearInterval(t);
  }, []);
}

/** The doctor check list (setup checklist step 1 + Settings → Doctor).
 * `onChange` fires after an install or a sign-in finished, so the checklist
 * can re-read its steps. */
export function DoctorList({ reprobeKey, onChange }: { reprobeKey?: number; onChange?(): void }) {
  const [doctor, setDoctor] = useState<DoctorPayload | null>(lastDoctor);
  const [error, setError] = useState("");
  // Bumped when the install window finishes, so the list re-probes itself.
  const [installed, setInstalled] = useState(0);

  useEffect(() => {
    let live = true;
    (async () => {
      const reprobe = (reprobeKey || 0) > 0 || installed > 0;
      try {
        const d = await api<DoctorPayload>("/api/doctor" + (reprobe ? "?refresh=1" : ""));
        if (!live) return;
        lastDoctor = d;
        useDoctorWarnStore.getState()._setFailing(!(d && d.ok));
        setDoctor(d);
        setError("");
        if (reprobe) onChange?.();
      } catch (e) {
        if (live) setError((e as Error).message);
      }
    })();
    return () => {
      live = false;
    };
    // onChange is a notification, not an input: re-running the probe because
    // the parent re-rendered with a new closure would re-probe forever.
  }, [reprobeKey, installed]);

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
