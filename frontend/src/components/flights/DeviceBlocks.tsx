/** Device drop targets (docs/flights.md "Round 15" → `objects`, reworked in
 * round-consolidate): nothing device-related stands on the board. While a
 * session's chain is dragged by its frame bar, the sky shows each device of
 * yours but this one (`GET /api/devices` minus `self`) as a labelled target
 * along the view's top edge — "Move to Ethan's Mac mini · 4 sessions there"
 * (`DeviceDock`, drawn by SkyView over the viewport, in screen px). Dropping
 * the chain on one asks inline — never a window dialog (the desktop app has
 * none) — and then calls the existing move route, `POST
 * /api/instances/{title}/move {device, force}` (api/skyApi
 * `moveSessionToDevice`): the agent pauses while the session moves. A refusal
 * shows the route's own sentence (lib/sessionActions `moveErrorText`); a
 * server that only moves folders on this machine answers `bad_dest`, which
 * the target names plainly.
 *
 * The host (SkyView) hands the dock the devices, the target under the
 * pointer (`over`, a `device:<name>` key), the dropped title per key
 * (`dropped`) and hears `onDone(key)` when a question is answered either
 * way. The look is SKY_OBJECT_STYLE's (DeviceBlocks.css): a calm frame, one
 * hairline, a 13px / 600 primary line and a muted 11.5px second one. */

import { useEffect, useState } from "react";
import { moveSessionToDevice } from "../../api/skyApi";
import type { Device } from "../../api/types";
import { moveErrorText, moveRefusal } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { windowName } from "../../lib/windowName";
import "./DeviceBlocks.css";

export interface DeviceBlockProps {
  device: Device;
  /** The session title dropped on it ("" while nothing is asked). */
  dropped?: string;
  onDone?(): void;
  /** A dragged chain is over it: it lights. */
  target?: boolean;
  /** Its element, for the host's hit test (screen px). */
  targetRef?(el: HTMLElement | null): void;
  /** Older hosts (the registry's device entry) pass these; the target is
   * never dragged or measured — it is not a board object any more. */
  onHeadDown?(e: unknown): void;
  draggable?: boolean;
  onMeasure?(w: number, h: number): void;
}

/** What a server that cannot move a session to another device says. */
export const NO_DEVICE_MOVE = "This server can't move a session to another device yet — it moves folders on this machine only.";

/** The sentence a failed device move shows: the route's, or ours for a
 * server that has no device target. */
export function deviceMoveError(err: unknown): string {
  const code = moveRefusal(err);
  if (code === "bad_dest") return NO_DEVICE_MOVE;
  return moveErrorText(err);
}

/** Why a device can't take a session now, or "". */
export function deviceBlockReason(d: Pick<Device, "connected" | "reachable">): string {
  if (!d.connected) return "Not connected — pair or wake it in Settings › Your devices.";
  return "";
}

/** A device's name as people read it. */
export const deviceName = (d: Pick<Device, "host" | "device">) => d.host || d.device;

/** The target's second line: "4 sessions there · macOS", or why it can't. */
export function deviceTargetWords(d: Pick<Device, "sessions" | "os" | "connected">): string {
  if (!d.connected) return "Not connected";
  const n = typeof d.sessions === "number" ? (d.sessions === 0 ? "No sessions there" : d.sessions + (d.sessions === 1 ? " session there" : " sessions there")) : "";
  return [n, d.os || ""].filter(Boolean).join(" · ") || "Drop to move the session here";
}

/** One device as a drop target — and, once a chain was dropped on it, the
 * inline question. */
export function DeviceBlock({ device, dropped = "", onDone, target = false, targetRef }: DeviceBlockProps) {
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    setErr("");
    setForce(false);
  }, [dropped]);
  const reason = dropped ? deviceBlockReason(device) : "";
  const name = deviceName(device);
  const move = async () => {
    if (!dropped || reason || busy) return;
    setBusy(true);
    setErr("");
    try {
      await moveSessionToDevice(dropped, device.device, force);
      toast(`Moving “${windowName(dropped)}” to ${name}…`);
      onDone?.();
    } catch (e) {
      setErr(deviceMoveError(e));
    } finally {
      setBusy(false);
    }
  };
  const state = device.connected ? "on" : "off";
  return (
    <div
      ref={targetRef}
      className={"sky-device" + (target ? " is-over" : "") + (dropped ? " is-asking" : "")}
      data-device={device.device}
      data-state={state}
      role="group"
      aria-label={"Move to " + name + " — drop the chain here"}
    >
      <div className="sky-device-bar">
        <span className={"sky-device-dot is-" + state} title={device.connected ? "connected" : "not connected"} />
        <span className="sky-device-title" title={device.device}>
          Move to {name}
        </span>
      </div>
      <div className="sky-device-sub">{deviceTargetWords(device)}</div>
      {dropped && (
        <form
          className="sky-device-ask fl-form"
          data-no-drag
          onSubmit={(e) => {
            e.preventDefault();
            void move();
          }}
        >
          <p>
            Move <strong>{windowName(dropped)}</strong> to {name}? Its agent pauses while it moves.
          </p>
          {reason ? (
            <p className="sky-device-err">{reason}</p>
          ) : (
            <>
              <label className="sky-device-choice">
                <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> Even if it is mid-turn
              </label>
              {err && <p className="sky-device-err">{err}</p>}
            </>
          )}
          <div className="sky-device-btns">
            {!reason && (
              <button type="submit" className="fl-btn btn-primary" disabled={busy}>
                {busy ? "Moving…" : "Move"}
              </button>
            )}
            <button type="button" className="test-btn" onClick={onDone}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

export interface DeviceDockProps {
  devices: Device[];
  /** A session's chain is in the hand: every device shows as a target. */
  dragging: boolean;
  /** The target under the pointer (`device:<name>`), "" for none. */
  over: string;
  /** What was dropped where (`device:<name>` → title); the dock keeps only
   * the asked device while its question is open. */
  dropped: Record<string, string>;
  keyOf(device: string): string;
  onDone(key: string): void;
  targetRef(key: string, el: HTMLElement | null): void;
}

/** The devices as drop targets along the sky's top edge: shown only while a
 * session's chain is dragged, or while a drop's question is open. */
export function DeviceDock({ devices, dragging, over, dropped, keyOf, onDone, targetRef }: DeviceDockProps) {
  const asked = devices.filter((d) => dropped[keyOf(d.device)]);
  const shown = dragging ? devices : asked;
  if (!shown.length) return null;
  return (
    <div className={"sky-device-dock" + (dragging ? " is-dragging" : "")} role="region" aria-label="Move the session to another device" data-no-drag>
      {dragging && <span className="sky-device-dock-hint">Drop the chain on a device to move the session there</span>}
      <div className="sky-device-dock-row">
        {shown.map((d) => {
          const key = keyOf(d.device);
          return <DeviceBlock key={key} device={d} dropped={dropped[key] || ""} target={over === key} onDone={() => onDone(key)} targetRef={(el) => targetRef(key, el)} />;
        })}
      </div>
    </div>
  );
}
