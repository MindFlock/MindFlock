/** Settings → Devices pieces that get a new computer ready to work.
 *
 *  - NewComputerLine: beside a live "Add a device" code, one copyable line
 *    that installs MindFlock on a brand-new computer (pinned to THIS device's
 *    version) and joins it — `install.sh --join` — plus the desktop-app
 *    equivalent (download, then paste the code).
 *  - useFleetReadiness + ReadinessLine: each member's own summary of itself
 *    (deps, agent sign-in, push, Tailscale key) under its row, read-only.
 *    The fix always runs on that device.
 *  - SyncedAgentInstall: settings sync held back a default agent this
 *    computer doesn't have — install it here in one click (the doctor's
 *    install terminal), then sync applies it.
 *
 * Kept out of Devices.tsx so that screen only gains three one-line mounts. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { hasSyncedAgentStep, readinessLine, type MemberReadiness } from "../../../lib/onboarding";
import { toast } from "../../../lib/toast";
import { InstallMissing, type InstallStep } from "../../dialogs/InstallTerminal";

interface Bootstrap {
  line: string;
  ref: string;
  pinned: boolean;
  code: string;
  desktop: { download: string; paste: string };
}

/** The one-line install-and-join for the invite code shown. */
export function NewComputerLine({ code }: { code: string }) {
  const [b, setB] = useState<Bootstrap | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open || !code) return;
    let live = true;
    api<Bootstrap>("/api/fleet/bootstrap", { json: { code } })
      .then((r) => live && setB(r))
      .catch((e) => live && toast((e as Error).message));
    return () => {
      live = false;
    };
  }, [open, code]);

  if (!open) {
    return (
      <button
        type="button"
        className="linklike devices-bootstrap-open"
        id="devices-bootstrap-open"
        onClick={() => setOpen(true)}
      >
        New computer with nothing installed yet?
      </button>
    );
  }
  if (!b) return <span className="set-hint">Making the line…</span>;
  return (
    <div className="devices-bootstrap" id="devices-bootstrap">
      <span className="set-hint">
        On the new computer (macOS, Linux or WSL), paste this into a terminal — it installs MindFlock{" "}
        {b.pinned ? b.ref + " (this computer's version)" : "(main — this is a development build)"}, signs
        in to Tailscale if needed, and joins:
      </span>
      <div className="devices-command">
        <code id="devices-bootstrap-line">{b.line}</code>
        <button
          type="button"
          className="test-btn"
          id="devices-bootstrap-copy"
          onClick={() => copyText(b.line).then((ok) => toast(ok ? "Line copied" : "Copy failed"))}
        >
          Copy
        </button>
      </div>
      <span className="set-hint">
        Desktop app instead:{" "}
        <a href={b.desktop.download} target="_blank" rel="noopener noreferrer">
          download it
        </a>
        , then Settings → Devices → Paste a code: <code>{b.desktop.paste}</code>. If
        setting up takes longer than the code lives, the new computer asks this one to approve instead.
      </span>
    </div>
  );
}

/** Every member's readiness, refreshed while the screen is open. */
export function useFleetReadiness(active: boolean, members: string[]): Record<string, MemberReadiness> {
  const [byKey, setByKey] = useState<Record<string, MemberReadiness>>({});
  const sig = members.join(",");
  useEffect(() => {
    if (!active || !sig) return;
    let live = true;
    const load = async () => {
      try {
        const r = await api<{ members: Record<string, MemberReadiness> }>("/api/fleet/readiness");
        if (live) setByKey(r.members || {});
      } catch {
        /* the roster still shows; readiness is extra */
      }
    };
    void load();
    const t = setInterval(() => {
      if (!document.hidden) void load();
    }, 60000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [active, sig]);
  return byKey;
}

/** The read-only readiness line under a member's row. */
export function ReadinessLine({ r, self }: { r?: MemberReadiness; self: boolean }) {
  const line = readinessLine(r);
  if (!line) return null;
  const fixes = r?.fixes || [];
  return (
    <span
      className={"devices-note devices-readiness" + (line.warn ? " warn" : "")}
      title={fixes.length ? (self ? "Here: " : "On that computer: ") + fixes.join("; ") : undefined}
    >
      {line.text}
      {line.warn && !self ? " — fix it on that computer" : ""}
    </span>
  );
}

/** "Install it here" for agent CLIs settings sync is holding back. */
export function SyncedAgentInstall({ onDone }: { onDone(): void }) {
  const [steps, setSteps] = useState<InstallStep[] | null>(null);
  const load = useCallback(async () => {
    try {
      const d = await api<{ install?: { steps?: InstallStep[] } }>("/api/doctor?refresh=1");
      setSteps(d.install?.steps || []);
    } catch {
      setSteps([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  if (!steps || !hasSyncedAgentStep(steps)) return null;
  return (
    <InstallMissing
      steps={steps}
      onDone={() => {
        void (async () => {
          // The CLI is there now: a sync pass adopts what was held back.
          try {
            await api("/api/settings/sync/now", { method: "POST" });
          } catch {
            /* the next pass (≤30 s) applies it anyway */
          }
          await load();
          onDone();
        })();
      }}
    />
  );
}
