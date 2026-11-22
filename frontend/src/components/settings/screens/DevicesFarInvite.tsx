/**
 * Settings → Your devices → Add a device → "It isn't on my network": a long
 * one-time code (`mfd1:…`, copyable, with a QR) for a computer that has
 * nothing set up — no Tailscale — to paste into its own Your devices screen
 * (docs/devices-anywhere.md §7). The code works through this computer's
 * private tunnel, so "Reach my devices from anywhere" must be on here: when
 * it is off the button first asks (an inline confirm — the desktop app has no
 * window.confirm) and turns it on with the code.
 *
 * Self-contained: GET/POST/DELETE /api/devices/anywhere/invite. Polls while a
 * code is live, so a used or restarted-away code disappears on its own.
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import {
  FAR_ADD_HINT,
  FAR_ADD_LABEL,
  FAR_TURN_ON_BODY,
  FAR_TURN_ON_TITLE,
  farInviteLine,
  farSafetyLine,
  type AnywhereInviteState,
} from "../../../lib/devicesAnywhere";
import { toast } from "../../../lib/toast";
import { useNow } from "../../../lib/useNow";
import { InlineConfirm } from "../useSettings";

const PATH = "/api/devices/anywhere/invite";
const POLL_MS = 4000;

export function DevicesFarInvite({ active, disabled }: { active: boolean; disabled?: boolean }) {
  const [st, setSt] = useState<AnywhereInviteState | null>(null);
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSt(await api<AnywhereInviteState>(PATH));
    } catch {
      /* an older server: no far-away codes */
    }
  }, []);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  const live = !!st?.invite;
  useEffect(() => {
    if (!active || !live) return;
    const t = setInterval(() => {
      if (!document.hidden) void load();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [active, live, load]);

  const now = useNow(live ? 1000 : false) / 1000;

  const make = async (turnOn: boolean) => {
    setBusy(true);
    try {
      setSt(await api<AnywhereInviteState>(PATH, { json: { turn_on: turnOn } }));
      setAsk(false);
    } catch (e) {
      const body = e instanceof ApiError ? (e.body as { needs_switch?: boolean } | null) : null;
      if (body?.needs_switch) setAsk(true);
      else toast(e instanceof Error ? e.message : String(e), { duration: 8000 });
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    try {
      setSt(await api<AnywhereInviteState>(PATH, { method: "DELETE" }));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!st) return null;
  return (
    <div id="devices-far" data-control="devices-anywhere-invite">
      {st.invite ? renderInvite(st.invite) : renderAdd(st.gone)}
    </div>
  );

  function renderInvite(inv: NonNullable<AnywhereInviteState["invite"]>) {
    return (
      <div className="devices-invite devices-far-invite" id="devices-far-invite">
        <span className="set-hint">On the new computer: open MindFlock → Settings → Your devices, paste this code into “Paste a code”, then Join.</span>
        <div className="devices-command devices-far-code-row">
          <code className="devices-far-code" id="devices-far-code">
            {inv.code}
          </code>
          <button
            type="button"
            className="test-btn"
            id="devices-far-copy"
            onClick={() => copyText(inv.code).then((ok) => toast(ok ? "Code copied" : "Copy failed"))}
          >
            Copy
          </button>
        </div>
        {inv.qr_svg && (
          <div className="devices-far-qr" aria-label="The code as a QR" dangerouslySetInnerHTML={{ __html: inv.qr_svg }} />
        )}
        {inv.safety && (
          <span className="devices-far-safety" id="devices-far-safety">
            {farSafetyLine(inv)}
          </span>
        )}
        <span className="set-hint" id="devices-far-countdown">
          {farInviteLine(inv, now)} · Anyone with this code can join your devices — send it only to yourself.
        </span>
        <button
          type="button"
          className="test-btn devices-self-start"
          id="devices-far-cancel"
          disabled={busy}
          onClick={() => void cancel()}
        >
          Cancel
        </button>
      </div>
    );
  }

  function renderAdd(gone: string) {
    return (
      <div className="devices-add devices-far-add">
        <button
          type="button"
          className="test-btn"
          id="devices-far-new"
          disabled={busy || disabled || ask}
          onClick={() => void make(false)}
        >
          {busy ? "Starting the private tunnel…" : FAR_ADD_LABEL}
        </button>
        <span className="set-hint">{gone || FAR_ADD_HINT}</span>
        {ask && (
          <InlineConfirm
            id="devices-far-turn-on"
            title={FAR_TURN_ON_TITLE}
            body={FAR_TURN_ON_BODY}
            confirmLabel={busy ? "Turning it on…" : "Turn it on and make a code"}
            busy={busy}
            onConfirm={() => void make(true)}
            onCancel={() => setAsk(false)}
          />
        )}
      </div>
    );
  }
}
