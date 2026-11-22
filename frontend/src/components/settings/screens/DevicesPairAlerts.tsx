/**
 * Settings → Your devices: a new device pairing through the private tunnel
 * right now ("pairing now" — not one of your devices yet), and the alarm a
 * pairing code raises when it was used but the join didn't finish, or two
 * computers used it (docs/devices-anywhere.md §7). Whoever used it holds your
 * devices' key, so the row offers **Change key** (every device gets a new key
 * and new access tokens) and **It was me**. Drawn from `GET /api/fleet`'s
 * `pairing`; the buttons call /api/devices/anywhere/pair-alerts.
 */
import { useState } from "react";
import { api } from "../../../api/client";
import type { FleetPairing } from "../../../api/types";
import { toast } from "../../../lib/toast";
import { InlineConfirm } from "../useSettings";

const PATH = "/api/devices/anywhere/pair-alerts";

export function DevicesPairAlerts({ pairing, reload }: { pairing?: FleetPairing; reload(): void }) {
  const [busy, setBusy] = useState("");
  const [ask, setAsk] = useState(false);
  const alerts = pairing?.alerts || [];
  const pending = pairing?.pending || [];
  if (!alerts.length && !pending.length) return null;

  const act = async (key: string, fn: () => Promise<unknown>, ok: (r: unknown) => string) => {
    setBusy(key);
    try {
      const r = await fn();
      toast(ok(r), { duration: 8000 });
      setAsk(false);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { duration: 8000 });
    } finally {
      setBusy("");
      reload();
    }
  };

  const changeKey = () =>
    act(
      "key",
      () => api<{ rekeyed?: string[]; missed?: string[] }>(PATH + "/change-key", { json: {} }),
      (r) => {
        const out = (r || {}) as { rekeyed?: string[]; missed?: string[] };
        const n = out.rekeyed?.length || 0;
        const missed = out.missed?.length || 0;
        return (
          "Key changed" +
          (n ? " on " + n + (n === 1 ? " other device" : " other devices") : "") +
          (missed ? " — " + missed + " offline take it when they're next seen" : "")
        );
      }
    );

  return (
    <>
      {alerts.length > 0 && (
        <div className="devices-warn" id="devices-pair-alerts" role="alert">
          {alerts.map((a) => (
            <div key={a.id} className="devices-pair-alert" data-alert={a.id}>
              <p>{a.text}</p>
              <button
                type="button"
                className="test-btn"
                data-dismiss={a.id}
                disabled={!!busy}
                onClick={() =>
                  void act(
                    "dismiss:" + a.id,
                    () => api(PATH + "?id=" + encodeURIComponent(a.id), { method: "DELETE" }),
                    () => "Dismissed"
                  )
                }
              >
                It was me
              </button>
            </div>
          ))}
          <button
            type="button"
            className="test-btn"
            id="devices-pair-change-key"
            disabled={!!busy || ask}
            onClick={() => setAsk(true)}
          >
            Change key
          </button>
          {ask && (
            <InlineConfirm
              id="devices-pair-change-key-confirm"
              title="Change your devices' key now?"
              body="Every device gets a new key and new access tokens, so whoever used the code is locked out. A phone signs in again with the QR in Your phone; a device that's offline takes the new key when it's next seen."
              confirmLabel={busy === "key" ? "Changing…" : "Change key"}
              busy={busy === "key"}
              onConfirm={() => void changeKey()}
              onCancel={() => setAsk(false)}
            />
          )}
        </div>
      )}
      {pending.length > 0 && (
        <div className="devices-note" id="devices-pair-pending" role="status">
          {pending.map((p) => (
            <p key={p.device} data-pending={p.device}>
              {(p.host || p.device) + " is pairing now — not one of your devices until it confirms."}
            </p>
          ))}
        </div>
      )}
    </>
  );
}
