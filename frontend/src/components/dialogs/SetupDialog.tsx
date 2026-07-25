/** C2 — first-run setup checklist + shared doctor renderers (port of app.js
 * section 15). Exports:
 *   SetupDialog     — the reopenable Setup modal
 *   SetupChecklist  — the ①②③ checklist (also used by the grid empty state)
 *   DoctorList      — the check list (also used by Settings → Doctor)
 *   useDoctorWarn   — F8 warn-chip state (sidebar chip reads it)
 *   useDoctorAutoShow — headless: load + 5-min doctor probe, auto-open rules */

import { useCallback, useEffect, useState } from "react";
import { create } from "zustand";
import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";

// --- localStorage flags (same keys as vanilla) -------------------------------

export function setupDismissed(): boolean {
  try {
    return localStorage.getItem("mf_setup_done") === "1";
  } catch {
    return false;
  }
}
function dismissSetup() {
  try {
    localStorage.setItem("mf_setup_done", "1");
  } catch {
    /* storage unavailable */
  }
}
function everCreated(): boolean {
  try {
    return localStorage.getItem("mf_ever_created") === "1";
  } catch {
    return false;
  }
}
export function markEverCreated() {
  try {
    localStorage.setItem("mf_ever_created", "1");
  } catch {
    /* storage unavailable */
  }
}

// --- Doctor model -------------------------------------------------------------

export interface DoctorCheckItem {
  id?: string;
  label?: string;
  status?: "ok" | "info" | "warn" | "fail" | string;
  detail?: string;
  fix?: string;
}

export interface DoctorPayload {
  ok: boolean;
  checks?: DoctorCheckItem[];
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

let setupAutoShown = false; // auto-open the setup dialog at most once per load

/** Headless doctor probe: on load + every 5 minutes (never on the 4s poll).
 * Failing required tools auto-open the setup checklist once per load. */
export function useDoctorAutoShow() {
  useEffect(() => {
    const check = async () => {
      try {
        const d = await api<DoctorPayload>("/api/doctor");
        lastDoctor = d;
        useDoctorWarnStore.getState()._setFailing(!(d && d.ok));
      } catch {
        /* unreachable backend is the conn-banner's job */
      }
      if (useDoctorWarnStore.getState().failing && !setupAutoShown) {
        setupAutoShown = true;
        useUi.getState().openDialogFor("setup");
      }
    };
    check();
    const t = setInterval(() => {
      if (!document.hidden) check();
    }, 300000);
    return () => clearInterval(t);
  }, []);
}

/** The doctor check list (setup panel + Settings → Doctor). */
export function DoctorList({ reprobeKey, autoCollapse }: { reprobeKey?: number; autoCollapse?: boolean }) {
  const [doctor, setDoctor] = useState<DoctorPayload | null>(lastDoctor);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    (async () => {
      const reprobe = (reprobeKey || 0) > 0;
      try {
        const d = await api<DoctorPayload>("/api/doctor" + (reprobe ? "?refresh=1" : ""));
        if (!live) return;
        lastDoctor = d;
        setDoctor(d);
        setError("");
        if (autoCollapse && d.ok && everCreated()) dismissSetup();
      } catch (e) {
        if (live) setError((e as Error).message);
      }
    })();
    return () => {
      live = false;
    };
  }, [reprobeKey, autoCollapse]);

  if (error) return <p className="error">doctor failed: {error}</p>;
  if (!doctor) return <p className="muted">Checking dependencies…</p>;
  if (!doctor.checks || !doctor.checks.length) return <p className="muted">doctor unavailable</p>;
  return (
    <ul className="doctor-list">
      {doctor.checks.map((c, i) => (
        <li key={c.id || i} className={"doctor-check st-" + (c.status || "info")}>
          <span className="doctor-ico">{DOCTOR_ICON[c.status || ""] || "•"}</span>
          <span className="doctor-label">{c.label || c.id || ""}</span>
          <span className="doctor-detail">
            {c.detail || ""}
            {c.fix && c.status !== "ok" && <span className="doctor-fix"> fix: {c.fix}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

// --- Account tests -------------------------------------------------------------

function TestResult({ state }: { state: { testing: boolean; ok?: boolean; msg?: string } }) {
  if (state.testing) return <span className="test-result">testing…</span>;
  if (state.msg === undefined) return <span className="test-result" />;
  return (
    <span className={"test-result " + (state.ok ? "ok" : "bad")}>
      {(state.ok ? "✓ " : "✗ ") + state.msg}
    </span>
  );
}

type TestState = { testing: boolean; ok?: boolean; msg?: string };
const idleTest: TestState = { testing: false };

/** The ①②③ checklist (empty-state card + the Setup dialog). */
export function SetupChecklist({ standalone }: { standalone?: boolean }) {
  const [reprobeKey, setReprobeKey] = useState(0);
  const [gh, setGh] = useState<TestState>(idleTest);
  const [sc, setSc] = useState<TestState>(idleTest);
  const [agent, setAgent] = useState<TestState>(idleTest);
  const [scToken, setScToken] = useState("");

  const closeSetup = () => {
    if (useUi.getState().openDialog === "setup") useUi.getState().closeDialog();
  };

  const testGithub = useCallback(async () => {
    setGh({ testing: true });
    try {
      const r = await api<Record<string, unknown>>("/api/settings/test/github", { method: "POST" });
      const bits = ["token: " + (r?.token_source || "none")];
      if (r?.gh_installed) bits.push(r.gh_authenticated ? "gh authenticated" : "gh not authenticated");
      else bits.push("gh not installed");
      if (r?.detail) bits.push(String(r.detail));
      setGh({ testing: false, ok: !!r?.ok, msg: bits.join(" · ") });
    } catch (e) {
      setGh({ testing: false, ok: false, msg: (e as Error).message });
    }
  }, []);

  const testShortcut = useCallback(async () => {
    setSc({ testing: true });
    const tok = scToken.trim();
    try {
      const r = await api<Record<string, unknown>>("/api/settings/test/shortcut", {
        json: { api_token: tok || "" },
      });
      if (r?.ok) {
        setSc({
          testing: false,
          ok: true,
          msg: "Shortcut OK — " + (r.name || r.mention_name || r.member_id),
        });
        toast("Shortcut token OK" + (r.name ? " — " + r.name : ""));
        if (tok) {
          // Persist through the normal settings path (never echoed back).
          try {
            await api("/api/settings", { json: { shortcut: { api_token: tok } } });
          } catch {
            /* the Settings screen remains the fallback */
          }
          setScToken("");
        }
        return;
      }
      setSc({ testing: false, ok: false, msg: String(r?.error || "test failed") });
    } catch (e) {
      setSc({ testing: false, ok: false, msg: (e as Error).message });
    }
  }, [scToken]);

  const testAgent = useCallback(async () => {
    setAgent({ testing: true });
    try {
      const r = await api<{ ok?: boolean; cli?: { detail?: string }; auth?: { detail?: string } }>(
        "/api/settings/test/agent",
        { method: "POST" }
      );
      const bits: string[] = [];
      if (r?.cli?.detail) bits.push(r.cli.detail);
      if (r?.auth?.detail) bits.push(r.auth.detail);
      setAgent({
        testing: false,
        ok: !!r?.ok,
        msg: bits.join(" · ") || (r?.ok ? "agent CLI ready" : "agent CLI not ready"),
      });
    } catch (e) {
      setAgent({ testing: false, ok: false, msg: (e as Error).message });
    }
  }, []);

  return (
    <>
      <div className="setup-step">
        <h3>
          <span className="setup-num">①</span> Dependencies
        </h3>
        <div className="setup-doctor">
          <DoctorList reprobeKey={reprobeKey} autoCollapse={!!standalone} />
        </div>
        <div className="setup-actions">
          <button type="button" className="setup-recheck" onClick={(e) => { e.stopPropagation(); setReprobeKey((k) => k + 1); }}>
            Re-check
          </button>
        </div>
      </div>
      <div className="setup-step">
        <h3>
          <span className="setup-num">②</span> Accounts
        </h3>
        <div className="setup-acct-row">
          <button type="button" className="setup-test-github" onClick={(e) => { e.stopPropagation(); testGithub(); }}>
            Test GitHub
          </button>
          <TestResult state={gh} />
        </div>
        <div className="setup-acct-row setup-shortcut-row">
          <button type="button" className="setup-test-shortcut" onClick={(e) => { e.stopPropagation(); testShortcut(); }}>
            Test Shortcut
          </button>
          <input
            type="password"
            className="setup-shortcut-token"
            placeholder="Shortcut API token (optional)"
            autoComplete="off"
            title="Paste a Shortcut API token to test it — saved on success, never displayed. Leave empty to test the stored token."
            value={scToken}
            onChange={(e) => setScToken(e.target.value)}
            onClick={(e) => e.stopPropagation()}
          />
          <TestResult state={sc} />
        </div>
        <div className="setup-acct-row">
          <button type="button" className="setup-test-agent" onClick={(e) => { e.stopPropagation(); testAgent(); }}>
            Test agent CLI
          </button>
          <TestResult state={agent} />
        </div>
        <p className="muted setup-hint">
          Tokens live in{" "}
          <button
            type="button"
            className="setup-open-settings linklike"
            onClick={(e) => {
              e.stopPropagation();
              closeSetup();
              useUi.getState().openDialogFor("settings");
            }}
          >
            Open Settings
          </button>
        </p>
      </div>
      <div className="setup-step">
        <h3>
          <span className="setup-num">③</span> Create your first session
        </h3>
        <div className="setup-actions">
          <button
            type="button"
            className="setup-new"
            onClick={(e) => {
              e.stopPropagation();
              closeSetup();
              useUi.getState().openDialogFor("new-session");
            }}
          >
            + New session
          </button>
        </div>
      </div>
    </>
  );
}

export function SetupDialog() {
  const open = useUi((s) => s.openDialog === "setup");
  const closeDialog = useUi((s) => s.closeDialog);

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

  return (
    <div
      id="setup-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="setup-panel-dlg">
        <div className="ws-head">
          <h2>Setup</h2>
          <button type="button" id="setup-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div id="setup-dialog-body">
          <SetupChecklist />
        </div>
      </div>
    </div>
  );
}
