/** The shared phone link ("One link for all devices"): one Tailscale Service
 * URL that whichever of your devices is up answers, with its setup
 * checklist. Lives on Settings → Your devices — it is a thing your devices
 * share, not a phone setting; Your phone only shows the resulting URL/QR.
 *
 * Self-contained: reads GET /api/mobile (the `shared` half, plus the QR when
 * it already encodes the shared URL), saves general.shared_link through
 * POST /api/settings (takes effect on save — no restart), and re-checks via
 * POST /api/mobile/shared/recheck. */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { UrlList } from "./Mobile";

export type StepState = "ok" | "fail" | "unknown";

export interface SetupStep {
  id: "operator" | "tag" | "define" | "policy" | "approval" | "phone" | string;
  title: string;
  state: StepState;
  reason: string;
}

export interface SharedLinkState {
  enabled?: boolean;
  name?: string;
  service?: string;
  url?: string;
  advertised?: boolean;
  /** Tri-state: null = this device can't tell (rendered "unknown", never ✓). */
  approved?: boolean | null;
  routed?: boolean | null;
  defined?: boolean | null;
  tagged?: boolean | null;
  tags?: string[];
  tag?: string;
  machine?: { hostname: string; dns: string; ip: string; duplicate_of: string };
  error?: string;
  error_kind?: string;
  operator_fix?: string;
  /** The whole policy-file block (tagOwners, autoApprovers, grants, tests). */
  policy?: string;
  admin?: { machines: string; services: string; policy: string };
  steps?: SetupStep[];
  devices?: Array<{ device: string; host: string; reachable: boolean }>;
}

/** The slice of GET /api/mobile this card reads. */
interface SharedPayload {
  shared?: SharedLinkState;
  qr_svg?: string;
  qr_target?: string;
}

function CopyButton({ text, what }: { text: string; what: string }) {
  return (
    <button
      type="button"
      className="test-btn"
      onClick={() => copyText(text).then((ok) => toast(ok ? what + " copied" : "Copy failed"))}
    >
      Copy
    </button>
  );
}

/** A command or snippet to paste somewhere, with its Copy button. */
function Snippet({ text, what }: { text: string; what: string }) {
  return (
    <div className="sl-snippet">
      <pre>{text}</pre>
      <CopyButton text={text} what={what} />
    </div>
  );
}

function AdminLink({ href, children }: { href?: string; children: ReactNode }) {
  if (!href) return null;
  return (
    <a className="sl-admin-link" href={href} target="_blank" rel="noopener noreferrer">
      {children} ↗
    </a>
  );
}

const DEFAULT_SHARED_NAME = "mindflock";

/** The card: loads itself, and refetches when `active` flips on (the screen
 * was reopened) or `revision` changes (something else on the screen saved
 * the link — Host here, Match my other devices). */
export function SharedLinkCard({ active, revision = 0 }: { active: boolean; revision?: number }) {
  const [data, setData] = useState<SharedPayload | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      setData(await api<SharedPayload>("/api/mobile"));
      setError("");
    } catch {
      setError("Could not load the shared link.");
    }
  }, []);
  useEffect(() => {
    if (active) void load();
  }, [active, revision, load]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return null;
  const shared = data.shared || {};
  // The QR encodes the shared link once this device advertises it — then it
  // is also the one to test with (the checklist's last step shows it).
  const sharedQr =
    data.qr_svg && shared.url && data.qr_target?.startsWith(shared.url) ? data.qr_svg : undefined;
  return <SharedLink shared={shared} qrSvg={sharedQr} onChanged={load} onData={setData} />;
}

/** One phone URL for every device: each device that turns this on (with the
 * same name) advertises the same Tailscale Service, and Tailscale routes the
 * phone to whichever is up. Takes effect on save — no restart. */
export function SharedLink({
  shared,
  qrSvg,
  onChanged,
  onData,
}: {
  shared: SharedLinkState;
  qrSvg?: string;
  onChanged: () => void;
  onData: (d: SharedPayload) => void;
}) {
  const [name, setName] = useState(shared.name || DEFAULT_SHARED_NAME);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (shared.name) setName(shared.name);
  }, [shared.name]);

  const save = async (value: string) => {
    setBusy(true);
    setError("");
    try {
      await api("/api/settings", { json: { general: { shared_link: value } } });
    } catch (e) {
      setError((e as Error).message || "Could not save.");
    }
    setBusy(false);
    onChanged();
  };

  const recheck = async () => {
    setBusy(true);
    setError("");
    try {
      onData(await api<SharedPayload>("/api/mobile/shared/recheck", { method: "POST" }));
    } catch (e) {
      setError((e as Error).message || "Could not re-check.");
    }
    setBusy(false);
  };

  const on = !!shared.enabled;
  // While it's on, the server re-checks every minute; refresh what we show
  // at the same pace so a pending approval turns green without a click.
  useEffect(() => {
    if (!on) return;
    const t = window.setInterval(onChanged, 60_000);
    return () => window.clearInterval(t);
  }, [on, onChanged]);

  const others = shared.devices || [];
  return (
    <>
      <h3 className="set-section-title" id="devices-shared-link">One link for all devices</h3>
      <p className="set-hint">
        Turn this on with the same name on each of your machines. The phone then keeps one URL,
        and it opens on whichever machine is running, so it still works when this one is off.
      </p>
      <div
        className="set-row set-switch-row"
        title="Advertise this device as a host of the shared Tailscale Service"
      >
        <span className="set-label">Shared link</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            checked={on}
            disabled={busy}
            onChange={(e) => save(e.target.checked ? name || DEFAULT_SHARED_NAME : "")}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <label className="set-row">
        <span className="set-label">Name</span>
        <input
          type="text"
          value={name}
          disabled={busy}
          placeholder={DEFAULT_SHARED_NAME}
          onChange={(e) => setName(e.target.value.trim().toLowerCase())}
          onBlur={() => on && name && name !== shared.name && save(name)}
          onKeyDown={(e) => e.key === "Enter" && on && name && name !== shared.name && save(name)}
        />
        <span className="set-hint">Use the same name on every device.</span>
      </label>
      {error && <p className="error">{error}</p>}
      {on && (
        <>
          {shared.url && <UrlList urls={[{ label: "Shared URL", url: shared.url }]} />}
          <SetupChecklist shared={shared} qrSvg={qrSvg} busy={busy} onRecheck={recheck} />
          <p className="set-hint">
            {others.length
              ? "Also on this link: " +
                others.map((d) => d.host + (d.reachable ? "" : " (offline)")).join(", ") +
                "."
              : "No other device on this link yet. Turn it on, with the same name, on each machine."}{" "}
            Signing in once covers your devices and devices paired under
            Allow remote control (Advanced, below): the QR carries their keys too.
          </p>
        </>
      )}
    </>
  );
}

const MARK: Record<StepState, string> = { ok: "✓", fail: "✗", unknown: "?" };
const MARK_LABEL: Record<StepState, string> = {
  ok: "done",
  fail: "needs fixing",
  unknown: "unknown",
};

/** The shared link's setup, one step per thing a host needs, each with its
 * live state from the server, a one-line reason, and the exact fix. A step
 * that passes keeps its fix folded away; one that doesn't shows it. */
export function SetupChecklist({
  shared,
  qrSvg,
  busy,
  onRecheck,
}: {
  shared: SharedLinkState;
  qrSvg?: string;
  busy?: boolean;
  onRecheck: () => void;
}) {
  const steps = shared.steps || [];
  return (
    <div className="sl-setup">
      <div className="sl-setup-head">
        <span className="set-label">Setup</span>
        <button type="button" className="test-btn" disabled={busy} onClick={onRecheck}>
          {busy ? "Checking…" : "Re-check"}
        </button>
      </div>
      <ol className="sl-steps">
        {steps.map((s, i) => {
          const fix = <StepFix step={s} shared={shared} qrSvg={qrSvg} />;
          return (
            <li key={s.id} className={"sl-step sl-" + s.state} data-step={s.id}>
              <span className="sl-mark" title={MARK_LABEL[s.state]} aria-label={MARK_LABEL[s.state]}>
                {MARK[s.state] || "?"}
              </span>
              <div className="sl-body">
                <div className="sl-title">
                  {i + 1}. {s.title}
                </div>
                <div className="set-hint sl-reason">{s.reason}</div>
                {s.state === "ok" && s.id !== "phone" ? (
                  <details className="sl-fix">
                    <summary>Show how</summary>
                    {fix}
                  </details>
                ) : (
                  <div className="sl-fix">{fix}</div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function StepFix({ step, shared, qrSvg }: { step: SetupStep; shared: SharedLinkState; qrSvg?: string }) {
  const svc = shared.service || "svc:" + (shared.name || DEFAULT_SHARED_NAME);
  const name = shared.name || DEFAULT_SHARED_NAME;
  const tag = shared.tag || "tag:mindflock";
  const m = shared.machine;
  const admin = shared.admin;
  switch (step.id) {
    case "operator":
      if (shared.error_kind === "missing")
        return (
          <p className="set-hint">
            Install it from{" "}
            <a href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer">
              tailscale.com/download
            </a>
            , sign in, then press Re-check.
          </p>
        );
      return (
        <>
          <p className="set-hint">
            Lets MindFlock run <code>tailscale serve</code> as you. Run once in a terminal, then
            press Re-check:
          </p>
          <Snippet text={shared.operator_fix || "sudo tailscale set --operator=$USER"} what="Command" />
        </>
      );
    case "tag":
      return (
        <>
          <p className="set-hint">
            In the admin console's Machines page, open{" "}
            <strong>{m?.dns || m?.hostname || "this device"}</strong>
            {m?.ip ? (
              <>
                {" "}
                (<code>{m.ip}</code>)
              </>
            ) : null}{" "}
            → ⋯ → Edit ACL tags → add <code>{tag}</code>. Then ⋯ → <strong>Disable key expiry</strong>:
            tagging a device after it signed in keeps its key expiry, and when the key expires the
            device silently drops off the link.
          </p>
          {m?.duplicate_of && (
            <p className="set-hint sl-note">
              Tailscale named this device <code>{m.dns.split(".")[0]}</code> because another
              device is already <code>{m.duplicate_of}</code> — match it by its IP.
            </p>
          )}
          <AdminLink href={admin?.machines}>Open Machines</AdminLink>
        </>
      );
    case "define":
      return (
        <>
          <p className="set-hint">
            On the Services page, add a service named <code>{name}</code> (<code>{svc}</code>)
            with port <code>tcp:443</code>.
          </p>
          <AdminLink href={admin?.services}>Open Services</AdminLink>
        </>
      );
    case "policy":
      return (
        <>
          <p className="set-hint">
            One block for your access policy: every <code>{tag}</code> device is approved as a host
            of <code>{svc}</code> without a click, and your own devices and phone may reach each
            other's MindFlock (needed under any custom policy, such as <code>autogroup:self</code>)
            — you only, not everyone else on the tailnet. If it says REPLACE, put your Tailscale
            login where it shows the placeholder.
            No <code>tagOwners</code>, <code>autoApprovers</code>, <code>grants</code> or{" "}
            <code>tests</code> in your policy yet? Paste it whole. Already have one? Move the lines
            inside it into yours: a key can't appear twice.
          </p>
          {shared.policy && <Snippet text={shared.policy} what="Policy" />}
          <AdminLink href={admin?.policy}>Open Access controls</AdminLink>
        </>
      );
    case "approval":
      if (step.state === "ok") return null;
      return (
        <>
          <p className="set-hint">
            Approve this device under Services → <code>{svc}</code>, or rely on the policy above.
            Re-check re-advertises this device, which is what the auto-approver reacts to.
          </p>
          <AdminLink href={admin?.services}>Open Services</AdminLink>
        </>
      );
    case "phone":
      return (
        <>
          {qrSvg && (
            // Server-generated segno SVG (trusted) — the same QR as above.
            <div className="qr-card sl-qr" dangerouslySetInnerHTML={{ __html: qrSvg }} />
          )}
          <p className="set-hint">
            With Tailscale on, open{" "}
            {shared.url ? (
              <a href={shared.url} target="_blank" rel="noopener noreferrer">
                {shared.url}
              </a>
            ) : (
              "the shared URL"
            )}{" "}
            on your phone. If it times out, Re-check here first.
          </p>
        </>
      );
    default:
      return null;
  }
}
