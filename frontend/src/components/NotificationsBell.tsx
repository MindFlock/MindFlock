/** Notification center bell (port of section 7): unread history badge vs the
 * amber needs-attention count, the popover with the pinned attention section
 * + history feed, and the desktop-notification toggle (shared addon API). */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useInstances, type EventEnvelope } from "../state/queries";
import { useUi } from "../state/store";
import { relTime } from "../lib/format";
import { selectSession } from "../lib/sessionActions";
import { attentionItems } from "./sidebar/ordering";

const NOTIF_CAP = 100;
const NOTIF_SEEN_KEY = "mf_notif_seen_ts";

/** Monochrome bell (fill=currentColor), never the 🔔 emoji. The emoji paints
 * its own colour from the OS emoji font — Apple Color Emoji renders a bright
 * yellow bell on macOS — so it both clashes with amber top bars (Goldfinch,
 * Toucan) and looks different per platform. currentColor follows --text/--muted
 * and stays identical on every OS. */
function BellGlyph({ size = 15 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" fill="currentColor">
      <path d="M12 2.2a1.3 1.3 0 0 1 1.3 1.3v.6a6 6 0 0 1 4.7 5.9v3.3l1.5 2.6a1 1 0 0 1-.9 1.5H5.4a1 1 0 0 1-.9-1.5L6 13.3V10a6 6 0 0 1 4.7-5.9v-.6A1.3 1.3 0 0 1 12 2.2z" />
      <path d="M9.6 19.2h4.8a2.4 2.4 0 0 1-4.8 0z" />
    </svg>
  );
}

interface Notif {
  seq: number;
  ts: number;
  session: string;
  text: string;
  cls: string;
}

/** Map a raw event envelope to a notification, or null to ignore the noise. */
export function notifFromEvent(env: EventEnvelope): { text: string; cls: string } | null {
  const d = env.data || {};
  switch (env.event) {
    case "session.created":
      return { text: "created", cls: "n-info" };
    case "session.create_failed":
      // A forced PR review / ticket start that dies during background
      // provisioning (clone, comment fetch) only emits this — without a case
      // here it was dropped, so the user saw an optimistic "starting…" toast
      // and then nothing. Surface the error so the failure is visible.
      return { text: "couldn't start — " + (d.error || "failed"), cls: "n-warn" };
    case "session.deleted":
      return { text: "deleted", cls: "n-muted" };
    case "session.activity_changed":
      if (env.new === "clarify") return { text: "needs your input", cls: "n-warn" };
      // idle / working / offline are chip colours, not news. `new === "idle"`
      // used to render "finished — now idle" here, with no rule gate and no
      // dedupe — so it logged a row at the end of every assistant turn, between
      // two prompts of a draining queue, and for a re-opened window whose agent
      // had run nothing at all. "Finished" is a claim about work, and the event
      // that can actually make it is session.turn_ended below.
      return null;
    case "session.turn_ended": {
      // Say how long, using the dwell the event carries — the whole claim of
      // this event is that the quiet lasted, so a bare "finished" throws away
      // the part that makes it trustworthy.
      const secs = Math.round(Number((d as { idle_for?: number }).idle_for) || 0);
      const held = secs >= 90 ? Math.round(secs / 60) + "m" : secs + "s";
      return { text: secs ? "finished — idle " + held : "finished", cls: "n-done" };
    }
    case "session.stage_changed":
      return { text: "stage → " + (env.new || ""), cls: "n-info" };
    case "session.budget_exceeded":
      return { text: "cost over budget ($" + (Number(d.cost) || 0).toFixed(2) + ")", cls: "n-warn" };
    case "session.prompt_sent":
      return { text: "auto-sent a queued prompt (" + (Number(d.remaining) || 0) + " left)", cls: "n-info" };
    case "session.setup_finished":
      return env.new === "ok"
        ? { text: "worktree setup finished", cls: "n-done" }
        : { text: "worktree setup FAILED — prompts held", cls: "n-warn" };
    case "session.check_finished":
      return env.new === "ok"
        ? { text: "checks passed ✓", cls: "n-done" }
        : { text: "checks failed ✗ (exit " + ((d as { rc?: number }).rc ?? "?") + ")", cls: "n-warn" };
    default:
      return null;
  }
}

interface NotifApi {
  state?: () => string;
  enable?: () => Promise<void> | void;
  disable?: () => void;
  unavailableReason?: string;
}

function notifApi(): NotifApi | null {
  const w = window as unknown as { mindflockAddons?: { notify?: NotifApi } };
  return w.mindflockAddons?.notify || null;
}

function notifState(): string {
  const api = notifApi();
  return api && typeof api.state === "function" ? api.state() : "unsupported";
}

export function NotificationsBell() {
  const { data: instances = [] } = useInstances();
  const [notifs, setNotifs] = useState<Notif[]>([]);
  const [open, setOpen] = useState(false);
  const [seenTs, setSeenTs] = useState(() => {
    try {
      return parseFloat(localStorage.getItem(NOTIF_SEEN_KEY) || "0") || 0;
    } catch {
      return 0;
    }
  });
  const [toggleState, setToggleState] = useState(notifState());
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);

  // Feed from the event bus; the replayed backlog IS the away-history,
  // deduped by seq. "Unread" keys on ts (seq resets on server restart).
  useEffect(() => {
    const bus = window.mindflock?.events;
    if (!bus) return;
    return bus.subscribe("*", (env) => {
      const n = notifFromEvent(env);
      if (!n) return;
      const seq = typeof env.seq === "number" ? env.seq : 0;
      setNotifs((prev) => {
        if (seq && prev.some((x) => x.seq === seq)) return prev;
        const next = [
          ...prev,
          { seq, ts: env.ts || Date.now() / 1000, session: env.session || "", ...n },
        ].sort((a, b) => a.ts - b.ts);
        return next.length > NOTIF_CAP ? next.slice(next.length - NOTIF_CAP) : next;
      });
    });
  }, []);

  // Keep the toggle in sync when Settings flips it while the panel is open.
  useEffect(() => {
    const on = () => setToggleState(notifState());
    document.addEventListener("mf-notify-state", on);
    return () => document.removeEventListener("mf-notify-state", on);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (popRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const attn = attentionItems(instances);
  const unread = notifs.filter((n) => n.ts > seenTs).length;
  const aliases = useUi((s) => s.aliases);

  const openPanel = () => {
    const newest = notifs.reduce((m, n) => Math.max(m, n.ts), seenTs);
    setSeenTs(newest);
    try {
      localStorage.setItem(NOTIF_SEEN_KEY, String(newest));
    } catch {
      /* storage unavailable */
    }
    setToggleState(notifState());
    setOpen(true);
  };

  const jump = (title: string) => {
    if (instances.some((x) => x.title === title)) {
      selectSession(title);
      setOpen(false);
    }
  };

  const rect = btnRef.current?.getBoundingClientRect();
  const popStyle = rect
    ? { top: rect.bottom + 6 + "px", left: Math.max(8, Math.min(rect.left, window.innerWidth - 328)) + "px" }
    : undefined;

  const shownAttn = attn.slice(0, 8);

  return (
    <>
      <button
        id="notif-btn"
        ref={btnRef}
        className={
          "tb-item" + (attn.length > 0 ? " has-attn" : "") + (attn.length === 0 && unread > 0 ? " has-unread" : "")
        }
        type="button"
        title="Notifications — what happened while you were away"
        aria-label="Notifications"
        onClick={(e) => {
          e.stopPropagation();
          open ? setOpen(false) : openPanel();
        }}
      >
        {/* Monochrome bell (see BellGlyph): never the 🔔 emoji, which paints its
            own colour and vanishes on a yellow/amber top bar. */}
        <BellGlyph />
        <span className={"notif-badge attn" + (attn.length === 0 ? " hidden" : "")} id="notif-badge">
          {attn.length > 99 ? "99+" : String(attn.length)}
        </span>
      </button>
      {open &&
        createPortal(
          <div className="notif-pop" ref={popRef} style={popStyle}>
            <div className="notif-head">
              <span>Notifications</span>
              <NotifToggle state={toggleState} onChange={() => setToggleState(notifState())} />
              <button
                className="notif-settings"
                type="button"
                title="Notification settings"
                onClick={() => {
                  setOpen(false);
                  useUi.getState().openDialogFor("settings", "notifications");
                }}
              >
                ⚙
              </button>
              <button className="notif-clear" type="button" onClick={() => setNotifs([])}>
                Clear
              </button>
            </div>
            {shownAttn.length > 0 && (
              <div className="notif-attn">
                <div className="notif-attn-head">Needs attention</div>
                {shownAttn.map((it) => (
                  <div
                    key={it.title + it.reason}
                    className={"attn-item p" + it.p}
                    data-attn={it.title}
                    onClick={() => jump(it.title)}
                  >
                    <span className="attn-dot" />
                    <span className="attn-title">{aliases[it.title] || it.title}</span>
                    <span className="attn-reason">{it.reason}</span>
                    {!!it.snippet && (
                      <div className="attn-snippet">
                        “{typeof it.snippet === "string" ? it.snippet : JSON.stringify(it.snippet)}”
                      </div>
                    )}
                  </div>
                ))}
                {attn.length > shownAttn.length && (
                  <div className="attn-more muted">+{attn.length - shownAttn.length} more</div>
                )}
              </div>
            )}
            {notifs.length ? (
              <div className="notif-list">
                {[...notifs].reverse().map((n, i) => (
                  <div
                    key={n.seq || n.ts + ":" + i}
                    className={"notif-item " + n.cls + (n.ts > seenTs ? " unread" : "")}
                    data-session={n.session}
                    onClick={() => jump(n.session)}
                  >
                    <span className="notif-sess">{aliases[n.session] || n.session || "—"}</span>
                    <span className="notif-text">{n.text}</span>
                    <span className="notif-time">{relTime(n.ts)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="notif-empty muted">No notifications yet.</div>
            )}
          </div>,
          document.body
        )}
    </>
  );
}

function NotifToggle({ state, onChange }: { state: string; onChange(): void }) {
  const api = notifApi();
  // Text-only label + a monochrome BellGlyph — no 🔔/🔕 emoji, which render as a
  // bright yellow bell in macOS's Apple Color Emoji font (differs per platform).
  const label =
    state === "on" ? "On" : state === "blocked" ? "Blocked" : state === "unsupported" ? "Unavailable" : "Off";
  const showBell = state === "on" || state === "off";
  const title =
    state === "blocked"
      ? "Notifications are blocked by the browser — allow them in this site's settings, then click again"
      : state === "unsupported"
        ? api?.unavailableReason || "Desktop notifications aren't available here"
        : state === "on"
          ? "Desktop notifications on — click to turn off"
          : "Turn on desktop notifications (clarify prompts, PR merges, budget overruns)";
  return (
    <button
      className={"notif-toggle" + (state === "on" ? " active" : "")}
      type="button"
      data-state={state}
      disabled={state === "unsupported"}
      title={title}
      onClick={async (e) => {
        e.stopPropagation();
        if (!api || state === "unsupported") return;
        if (state === "on") api.disable?.();
        else await api.enable?.();
        onChange();
      }}
    >
      {showBell && <BellGlyph size={12} />}
      {label}
    </button>
  );
}
