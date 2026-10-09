/** Settings → Mobile (partial 103 + loadMobile, section 21): /m URLs + QR,
 * the tailscale-mode toggle, the restart-to-apply flow, and the shared phone
 * link (one Tailscale Service URL that whichever device is up answers) with
 * its setup checklist. */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { useConfig } from "../../../state/queries";
import { useServerRestart } from "../useServerRestart";
import type { ScreenProps } from "../SettingsDialog";

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
  policy?: string;
  grants?: string;
  admin?: { machines: string; services: string; policy: string };
  steps?: SetupStep[];
  devices?: Array<{ device: string; host: string; reachable: boolean }>;
}

interface MobilePayload {
  shared?: SharedLinkState;
  serve_mode?: string;
  local_only?: boolean;
  qr_svg?: string;
  qr_target?: string;
  note?: string;
  urls?: Array<{ label: string; url: string }>;
  token?: string;
}

export const TOKEN_MASK = "••••••••••••••••";

export function Mobile(_: ScreenProps) {
  const { data: config } = useConfig();
  const [data, setData] = useState<MobilePayload | null>(null);
  const [error, setError] = useState("");
  const { restarting, restart } = useServerRestart();
  const [modeBusy, setModeBusy] = useState(false);
  const tailscale = config?.caps?.tailscale !== false;

  const load = useCallback(async () => {
    if (!tailscale) return;
    try {
      setData(await api<MobilePayload>("/api/mobile"));
      setError("");
    } catch {
      setError("Could not load mobile info.");
    }
  }, [tailscale]);

  useEffect(() => {
    load();
  }, [load]);

  const setMode = async (on: boolean) => {
    setModeBusy(true);
    let restarting = false;
    try {
      const res = await api<{ restarting?: boolean }>("/api/settings", {
        json: { general: { serve_mode: on ? "tailscale" : "local" } },
      });
      restarting = !!res?.restarting;
    } catch {
      /* revert via reload */
    }
    setModeBusy(false);
    // Turning tailscale mode on restarts the server by itself (which interface
    // uvicorn binds is fixed at boot). It's already going down — wait it out
    // and refresh the URLs/QR, rather than letting the reload below fail
    // against a port that is mid-re-exec.
    if (restarting) restart({ alreadyRequested: true, onBack: load });
    else load();
  };

  // Refresh the QR/URLs in place once the server is back — the new serve mode
  // changes them. No page reload here: that would close Settings, and this
  // flow is "apply one toggle", not "restart everything".
  const onRestart = () => restart({ onBack: load });

  // Saved choice not live yet (an explicit choice only — "" never nags).
  const pending = !!(data?.serve_mode && (data.serve_mode === "tailscale") === !!data.local_only);

  const shared = data?.shared || {};
  // The QR encodes the shared link once this device advertises it — then it
  // is also the one to test with (the checklist's last step shows it again).
  const sharedQr =
    data?.qr_svg && shared.url && data.qr_target?.startsWith(shared.url) ? data.qr_svg : undefined;

  return (
    <>
      <h3 className="set-section-title">Mobile</h3>
      <div className="caps-gate" data-caps-gate="tailscale">
        <p>
          Install <strong>Tailscale</strong> to get access to these features — it puts your
          phone and this machine on a private network, so you can drive your sessions from
          anywhere.
        </p>
        <p>
          Get it at{" "}
          <a href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer">
            tailscale.com/download
          </a>{" "}
          (sign in on both devices), then reopen this screen — the QR code and phone URLs
          appear here.
        </p>
      </div>
      <p className="set-hint">
        Open MindFlock on your phone. Scan the QR from a device on your Tailscale network, or
        use one of the URLs below.
      </p>
      <div id="mobile-body" className="mobile-body">
        {!tailscale ? null : error ? (
          <p className="error">{error}</p>
        ) : !data ? (
          <p className="set-hint">Loading…</p>
        ) : (
          <>
            <div
              className="set-row set-switch-row"
              title="Listen on this device's Tailscale addresses too, so phones on your tailnet can reach it (your LAN still can't)"
            >
              <span className="set-label">Tailscale mode</span>
              {/* label wraps only the switch, so clicking the row text no longer flips it */}
              <label className="ca-switch">
                <input
                  type="checkbox"
                  checked={data.serve_mode === "tailscale"}
                  disabled={modeBusy}
                  onChange={(e) => setMode(e.target.checked)}
                />
                <span className="ca-slider" />
              </label>
            </div>
            {pending && (
              <button
                type="button"
                className="test-btn mobile-restart"
                disabled={restarting}
                onClick={onRestart}
              >
                {restarting ? "Restarting…" : "Restart server to apply"}
              </button>
            )}
            {data.qr_svg && (
              // Server-generated segno SVG (trusted).
              <div id="mobile-qr" dangerouslySetInnerHTML={{ __html: data.qr_svg }} />
            )}
            {data.note && <p className="set-hint">{data.note}</p>}
            <UrlList urls={data.urls || []} />
            {data.token && <TokenField token={data.token} />}
            <SharedLink shared={shared} qrSvg={sharedQr} onChanged={load} onData={setData} />
          </>
        )}
      </div>
    </>
  );
}

/** Label/URL pairs in one grid, so every label shares a column and long URLs
 * wrap inside their own cell (stacked under the label when narrow). */
export function UrlList({ urls }: { urls: Array<{ label: string; url: string }> }) {
  if (!urls.length) return null;
  return (
    <dl className="mobile-urls">
      {urls.map((u) => (
        <div className="mobile-url-row" key={u.url}>
          <dt className="set-label">{u.label}</dt>
          <dd>
            <a href={u.url} target="_blank" rel="noopener noreferrer">
              {u.url}
            </a>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** The access token, masked until asked for: it is a bearer credential and
 * this screen gets screenshotted. Same Show/Copy pair as Settings → Security. */
export function TokenField({ token }: { token: string }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="set-row">
      <span className="set-label">Access token</span>
      <div className="token-reveal">
        <code>{shown ? token : TOKEN_MASK}</code>
        <button type="button" className="test-btn" onClick={() => setShown(!shown)}>
          {shown ? "Hide" : "Show"}
        </button>
        <CopyButton text={token} what="Access token" />
      </div>
      <span className="set-hint">Signs a browser in to this device — treat it like a password.</span>
    </div>
  );
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

/** One phone URL for every device: each device that turns this on (with the
 * same name) advertises the same Tailscale Service, and Tailscale routes the
 * phone to whichever is up. Takes effect on save — no restart. */
function SharedLink({
  shared,
  qrSvg,
  onChanged,
  onData,
}: {
  shared: SharedLinkState;
  qrSvg?: string;
  onChanged: () => void;
  onData: (d: MobilePayload) => void;
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
      onData(await api<MobilePayload>("/api/mobile/shared/recheck", { method: "POST" }));
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
      <h3 className="set-section-title">One link for all devices</h3>
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
            Sign-in carries across devices paired under Remote control: the QR carries their
            access tokens too.
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
            → ⋯ → Edit ACL tags → add <code>{tag}</code>.
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
            Add to your access policy so every <code>{tag}</code> device is approved as a host of{" "}
            <code>{svc}</code> without a click:
          </p>
          {shared.policy && <Snippet text={shared.policy} what="Policy" />}
          <p className="set-hint">
            Using a custom policy rather than the default allow-all? Phones must also be allowed to
            reach <code>{svc}</code> on <code>tcp:443</code>:
          </p>
          {shared.grants && <Snippet text={shared.grants} what="Grant" />}
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
