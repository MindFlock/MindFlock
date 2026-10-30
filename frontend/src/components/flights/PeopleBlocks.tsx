/** People blocks (docs/flights.md "Round 15" → `objects`): one sky object
 * per peer link (layout key `person:<link id>`, from `GET /api/peer` →
 * `links`, the same list the rail's "Shared with you" reads). Dropping a
 * session's line on one SHARES the session over that link — the existing
 * share op (lib/peerSessions `sharePeerSession`), with its gates kept: the
 * safety number must be verified ("It matches" in People) before anything
 * is shared, a full share needs the "I understand" tick (`canShare`), and
 * only a shareable session (`shareableSessions`) may be dropped. The block
 * asks inline (never a window dialog) and says **Shared** after, with the
 * sessions it already shares (`fetchPeerSessions`).
 *
 * The host (SkyView) hands the block the dropped title (`dropped`) and
 * hears `onDone` when the question is answered either way. */

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { Instance } from "../../api/types";
import { errMsg } from "../../lib/format";
import type { PeerLink } from "../../lib/peer";
import { MODE_CHOICES, canShare, fetchPeerSessions, sharePeerSession, shareableSessions, type PeerMode } from "../../lib/peerSessions";
import { toast } from "../../lib/toast";
import { windowName } from "../../lib/windowName";
import "./SkyObjects.css";

export interface PersonBlockProps {
  link: PeerLink;
  rows: Instance[];
  /** The session line dropped on the block ("" = nothing asked). */
  dropped?: string;
  onDone?(): void;
  /** A line is being dragged over it. */
  target?: boolean;
  onHeadDown?(e: ReactPointerEvent): void;
  draggable?: boolean;
  onMeasure?(w: number, h: number): void;
}

/** Why a dropped session can't be shared on this link, or "" when it can. */
export function shareBlockReason(link: Pick<PeerLink, "sas_verified" | "peer_name">, title: string, rows: Instance[]): string {
  if (!link.sas_verified) return "Press It matches in People first — sharing waits for the safety number.";
  if (!shareableSessions(rows).some((r) => r.title === title)) return "Only a session of this machine's, off the rail, can be shared.";
  return "";
}

const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, label, summary, [data-no-drag]");

export function PersonBlock({ link, rows, dropped = "", onDone, target = false, onHeadDown, draggable = true, onMeasure }: PersonBlockProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [shared, setShared] = useState<string[]>([]);
  const [mode, setMode] = useState<PeerMode>("gated");
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // What it already shares: asked once, and again after a share.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let on = true;
    fetchPeerSessions(link.link_id)
      .then((v) => on && setShared(v.mine.map((m) => m.title)))
      .catch(() => {});
    return () => {
      on = false;
    };
  }, [link.link_id, tick]);
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
    setUnderstood(false);
  }, [dropped]);
  const headDown = (e: ReactPointerEvent) => {
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const reason = dropped ? shareBlockReason(link, dropped, rows) : "";
  const ok = canShare({ title: dropped, mode, understood, busy });
  const share = async () => {
    if (!ok || reason) return;
    setBusy(true);
    setErr("");
    try {
      await sharePeerSession(link.link_id, dropped, mode);
      const name = windowName(dropped);
      toast(mode === "full" ? `${link.peer_name} can open ${name} now` : `Sharing a sandboxed copy of ${name} with ${link.peer_name}`);
      setTick((t) => t + 1);
      onDone?.();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const state = !link.connected ? "offline" : link.sas_verified ? "verified" : "unverified";
  return (
    <div
      ref={rootRef}
      className={"sky-block sky-person" + (target ? " is-merge-target" : "")}
      data-person={link.link_id}
      data-state={state}
      role="group"
      aria-label={link.peer_name + " — drop a line here to share its session"}
    >
      <div className="sky-block-head" data-sky-head title={draggable && onHeadDown ? "Drag to move" : undefined} onPointerDown={headDown}>
        <span className="sky-block-glyph" aria-hidden="true">
          ☺
        </span>
        <strong className="sky-block-name">{link.peer_name}</strong>
        <span className={"sky-block-dot is-" + state} title={state === "offline" ? "not connected" : state === "verified" ? "safety number verified" : "safety number not verified yet"} />
      </div>
      <div className="sky-block-body muted">
        {shared.length ? (
          <span className="sky-block-shared" title={shared.map(windowName).join(", ")}>
            Shared · {shared.length === 1 ? windowName(shared[0]) : shared.length + " sessions"}
          </span>
        ) : (
          <span>Drop a line to share it</span>
        )}
      </div>
      {dropped && (
        <form
          className="sky-block-ask fl-form"
          data-no-drag
          onSubmit={(e) => {
            e.preventDefault();
            void share();
          }}
        >
          <p>
            Share <strong>{windowName(dropped)}</strong> with {link.peer_name}?
          </p>
          {reason ? (
            <p className="sky-block-err">{reason}</p>
          ) : (
            <>
              {MODE_CHOICES.map((c) => (
                <label key={c.mode} className="sky-block-choice" title={c.hint}>
                  <input type="radio" name={"mode-" + link.link_id} checked={mode === c.mode} onChange={() => setMode(c.mode)} /> {c.label}
                </label>
              ))}
              {mode === "full" && (
                <label className="sky-block-choice">
                  <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} /> I understand they get the whole session
                </label>
              )}
              {err && <p className="sky-block-err">{err}</p>}
            </>
          )}
          <div className="sky-block-btns">
            {!reason && (
              <button type="submit" className="fl-btn btn-primary" disabled={!ok}>
                {busy ? "Sharing…" : "Share"}
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
