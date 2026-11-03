/** Device blocks (docs/flights.md "Round 15" → `objects`): one sky object
 * per device of yours but this one (layout key `device:<name>`, from `GET
 * /api/devices` minus `self`). Dropping a session's line on one asks inline
 * — never a window dialog (the desktop app has none) — and then calls the
 * existing move route, `POST /api/instances/{title}/move {device, force}`
 * (api/skyApi `moveSessionToDevice`): the agent pauses while the session
 * moves. A refusal shows the route's own sentence (lib/sessionActions
 * `moveErrorText`); a server that only moves folders on this machine
 * answers `bad_dest`, which the block names plainly.
 *
 * The host (SkyView) hands the block the dropped title (`dropped`) and
 * hears `onDone` when the question is answered either way. */

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { moveSessionToDevice } from "../../api/skyApi";
import type { Device } from "../../api/types";
import { moveErrorText, moveRefusal } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { windowName } from "../../lib/windowName";
import "./SkyObjects.css";

export interface DeviceBlockProps {
  device: Device;
  dropped?: string;
  onDone?(): void;
  target?: boolean;
  onHeadDown?(e: ReactPointerEvent): void;
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

const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, label, summary, [data-no-drag]");

export function DeviceBlock({ device, dropped = "", onDone, target = false, onHeadDown, draggable = true, onMeasure }: DeviceBlockProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const measureRef = useRef(onMeasure);
  measureRef.current = onMeasure;
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const read = () => {
      if (el.offsetWidth > 0 && el.offsetHeight > 0) measureRef.current?.(el.offsetWidth, el.offsetHeight);
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    setErr("");
    setForce(false);
  }, [dropped]);
  const headDown = (e: ReactPointerEvent) => {
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const reason = dropped ? deviceBlockReason(device) : "";
  const move = async () => {
    if (!dropped || reason || busy) return;
    setBusy(true);
    setErr("");
    try {
      await moveSessionToDevice(dropped, device.device, force);
      toast(`Moving “${windowName(dropped)}” to ${device.host || device.device}…`);
      onDone?.();
    } catch (e) {
      setErr(deviceMoveError(e));
    } finally {
      setBusy(false);
    }
  };
  const state = device.connected ? "on" : "off";
  const name = device.host || device.device;
  return (
    <div ref={rootRef} className={"sky-block sky-device" + (target ? " is-merge-target" : "")} data-device={device.device} data-state={state} role="group" aria-label={name + " — drop a chain here to move its session there"}>
      <div className="sky-block-head" data-sky-head title={draggable && onHeadDown ? "Drag to move" : undefined} onPointerDown={headDown}>
        <span className="sky-block-glyph" aria-hidden="true">
          ▣
        </span>
        <strong className="sky-block-name" title={device.device}>
          {name}
        </strong>
        <span className={"sky-block-dot is-" + state} title={device.connected ? "connected" : "not connected"} />
      </div>
      <div className="sky-block-body muted">
        {device.os ? device.os + " · " : ""}
        {typeof device.sessions === "number" ? device.sessions + (device.sessions === 1 ? " session" : " sessions") : "Drop a chain to move it here"}
      </div>
      {dropped && (
        <form
          className="sky-block-ask fl-form"
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
            <p className="sky-block-err">{reason}</p>
          ) : (
            <>
              <label className="sky-block-choice">
                <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> Even if it is mid-turn
              </label>
              {err && <p className="sky-block-err">{err}</p>}
            </>
          )}
          <div className="sky-block-btns">
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
