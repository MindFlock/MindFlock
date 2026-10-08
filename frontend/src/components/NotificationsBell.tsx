/** Notification center bell (port of section 7): the ONE place anything
 * waits on you, plus what happened while you were away.
 *
 * "Needs attention" merges the sidebar's attention items (an agent asking, a
 * broken session, failing checks, ready for a PR) with the server's waiting
 * rows (`GET /api/outbox`: an ask-first approval with its commit-message preview, a stuck group
 * line, a spent budget, a lead's plan or PR) — `needsAttention`, one row per
 * session — and the amber badge counts exactly that list. Below it, the
 * history feed. The desktop-notification switch lives in Settings →
 * Notifications (the ⚙ here goes there).
 *
 * Other surfaces open it with a DOM event, `mf-open-bell` (detail.title
 * optional: scroll that session's row into view and flash it) — the rail's
 * "— open the bell" lines and run toasts. A waiting row that navigates closes
 * it with `mf-close-bell`. A group's history row shows the group where it
 * lives (lib/showGroup.ts): its header on the rail, else — a split or
 * one-for-all group has none — its lead's Thread tab. */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useInstances, type EventEnvelope } from "../state/queries";
import { displayName, useUi } from "../state/store";
import { relTime } from "../lib/format";
import { selectSession } from "../lib/sessionActions";
import { attentionItems } from "./sidebar/ordering";
import { slotNumber, windowName } from "../lib/windowName";
import { childrenByParent, inFamily, messageNotif, workerOf, type MessageEventData } from "../lib/agentMessages";
import { openThread } from "../lib/flockActions";
import { AnswerStrip } from "./AnswerStrip";
import { runNote } from "../lib/runs";
import { ruleOn, runLookups, useNotifyConfig, useOutbox, useRuns } from "../state/runs";
import { needsAttention } from "./outbox/outbox";
import { WaitingRow } from "./outbox/WaitingRow";
import { showGroup } from "../lib/showGroup";
import { deviceEventNote } from "../lib/fleet";

const NOTIF_CAP = 100;
const NOTIF_SEEN_KEY = "mf_notif_seen_ts";
/** Rows of "Needs attention" shown before "+N more". */
const NEEDS_SHOWN = 6;

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
  /** A run event's group: the row reveals its header on the rail. */
  run?: string;
  /** A group's lead whose Thread holds the click (a plan, the one PR). */
  lead?: string;
  /** Same fact, same key — a replay under a new seq adds no second row. */
  dedupe?: string;
  /** The notify rule that gated it ("run_needs_you" rows point at a needs row). */
  rule?: string;
  /** A device.* row ("Your devices"): named "Devices", opens Settings → Devices. */
  device?: boolean;
}

/** A stage change, said as what happened. */
const STAGE_WORDS: Record<string, string> = {
  committed: "committed",
  pushed: "pushed",
  pr: "opened a PR",
  merged: "merged",
  interrupt: "pre-commit failed",
  precommit: "running pre-commit hooks",
};

/** A bell row as notifFromEvent produces it. `rule` names the notify rule that
 * gates it (the bell is the third, otherwise ungated channel). */
export interface NotifRow {
  text: string;
  cls: string;
  run?: string;
  lead?: string;
  dedupe?: string;
  rule?: string;
  device?: boolean;
}

/** Map a raw event envelope to a notification, or null to ignore the noise. */
export function notifFromEvent(env: EventEnvelope): NotifRow | null {
  const d = env.data || {};
  switch (env.event) {
    // Ship lanes: a group's escalation, a line shipped, a group finished. One
    // emitter on the server sends each once; `run.changed` is a refetch, not
    // news, and a "prompt" needs-you is the session's own clarify row above.
    case "run.needs_you":
    case "run.task_shipped":
    case "run.finished":
      return runNote(env.event, d, runLookups);
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
      return { text: STAGE_WORDS[String(env.new || "")] || String(env.new || ""), cls: "n-info" };
    case "session.budget_exceeded":
      return { text: "cost over budget ($" + (Number(d.cost) || 0).toFixed(2) + ")", cls: "n-warn" };
    case "session.prompt_sent":
      return { text: "sent the next queued prompt (" + (Number(d.remaining) || 0) + " left)", cls: "n-info" };
    case "session.setup_finished":
      return env.new === "ok"
        ? { text: "worktree setup finished", cls: "n-done" }
        : { text: "worktree setup FAILED — prompts held", cls: "n-warn" };
    case "session.check_finished":
      return env.new === "ok"
        ? { text: "checks passed ✓", cls: "n-done" }
        : { text: "checks failed ✗ (exit " + ((d as { rc?: number }).rc ?? "?") + ")", cls: "n-warn" };
    // Red zones (SPEC §3). `detail` is the server's own sentence ("blocked 3
    // edits to config.toml"), so the bell says what happened, not just that
    // something did.
    case "session.red_zone_blocked": {
      const detail = String((d as { detail?: string }).detail || "").trim();
      return { text: "zone — " + (detail || "blocked an edit"), cls: "n-warn" };
    }
    case "session.red_zone_breached": {
      const detail = String((d as { detail?: string }).detail || "").trim();
      return { text: "zone breached — " + (detail || "a protected file changed"), cls: "n-warn" };
    }
    case "session.red_zone_tampered": {
      const what = String((d as { what?: string }).what || "guard");
      return { text: "red-zone guard tampered (" + what + ")", cls: "n-warn" };
    }
    // Agent-to-agent traffic (MindFlock MCP): only a worker's REPORT is news
    // (done / blocked / failed, filed under the parent it reported to). Plain
    // messages between agents are toasted but stay out of the feed — the bell
    // has no dedupe, and an orchestrator's chatter would bury every row here.
    case "session.message":
      return messageNotif(d as MessageEventData, displayName);
    // Your devices (no session): a request to join, a join, a removal.
    case "device.join_requested":
    case "device.joined":
    case "device.removed": {
      const n = deviceEventNote(env.event, d);
      return n ? { text: n.text, cls: n.cls, device: true } : null;
    }
    default:
      return null;
  }
}

export function NotificationsBell() {
  const { data: instances = [] } = useInstances();
  // The server's waiting rows (`GET /api/outbox`) are half of "Needs
  // attention".
  const { data: outbox } = useOutbox();
  const { data: runs } = useRuns();
  const [notifs, setNotifs] = useState<Notif[]>([]);
  const [open, setOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [seenTs, setSeenTs] = useState(() => {
    try {
      return parseFloat(localStorage.getItem(NOTIF_SEEN_KEY) || "0") || 0;
    } catch {
      return 0;
    }
  });
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  /** When the panel last opened (performance.now()): the click that opened it
   * from elsewhere — a rail line, a toast — is still bubbling, and must not
   * read as a click outside it. */
  const openedAt = useRef(0);

  // Feed from the event bus; the replayed backlog IS the away-history,
  // deduped by seq. "Unread" keys on ts (seq resets on server restart).
  // The per-rule switches (Settings → Notifications), read for the rows that
  // name a rule — kept warm here because the bell is always mounted.
  useNotifyConfig();
  useEffect(() => {
    const bus = window.mindflock?.events;
    if (!bus) return;
    return bus.subscribe("*", (env) => {
      const n = notifFromEvent(env);
      if (!n) return;
      if (n.rule && !ruleOn(n.rule)) return;
      const seq = typeof env.seq === "number" ? env.seq : 0;
      setNotifs((prev) => {
        if (seq && prev.some((x) => x.seq === seq)) return prev;
        if (n.dedupe && prev.some((x) => x.dedupe === n.dedupe)) return prev;
        const next = [
          ...prev,
          { seq, ts: env.ts || Date.now() / 1000, session: env.session || "", ...n },
        ].sort((a, b) => a.ts - b.ts);
        return next.length > NOTIF_CAP ? next.slice(next.length - NOTIF_CAP) : next;
      });
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (e.timeStamp <= openedAt.current) return;
      // The path as it was when the click was dispatched, not `contains` on the
      // target: React commits between the root's listener and this one, so a
      // control the click itself replaced ("+N more", a preview's "edit") is
      // already detached here and would read as a click outside the panel.
      const path = e.composedPath();
      if ((popRef.current && path.includes(popRef.current)) || (btnRef.current && path.includes(btnRef.current)))
        return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) setOpen(false);
    };
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const families = useMemo(() => childrenByParent(instances), [instances]);
  const byTitle = useMemo(() => new Map(instances.map((i) => [i.title, i])), [instances]);
  // ONE list: the sessions' own attention items and the server's waiting rows,
  // one row per session (outbox.ts `needsAttention` says how they merge).
  const attn = needsAttention(attentionItems(instances), outbox?.groups?.waiting);
  // MindFlock MCP families: a worker's (or an orchestrator's) "needs your
  // answer" item names whose worker it is, and its strip can redirect the
  // question to the lead's Thread — answerable from any screen, two clicks.
  const familyOf = (title: string) => {
    const inst = byTitle.get(title);
    if (!inst || inst.device) return null;
    const parent = inst.parent && families.get(inst.parent)?.includes(inst) ? inst.parent : "";
    return inFamily(inst, !!parent, families.get(title)?.length ?? 0) ? { parent } : null;
  };
  const unread = notifs.filter((n) => n.ts > seenTs).length;
  // Rows are named by windowName, which reads the renames from the store: keep
  // a subscription so a rename repaints the open panel.
  useUi((s) => s.aliases);

  const openPanel = () => {
    const newest = notifs.reduce((m, n) => Math.max(m, n.ts), seenTs);
    setSeenTs(newest);
    try {
      localStorage.setItem(NOTIF_SEEN_KEY, String(newest));
    } catch {
      /* storage unavailable */
    }
    openedAt.current = performance.now();
    setOpen(true);
  };
  // The DOM-event listeners below are registered once; they reach the
  // current render's state through these refs.
  const openRef = useRef(openPanel);
  openRef.current = openPanel;
  const attnRef = useRef(attn);
  attnRef.current = attn;

  // `mf-open-bell` (detail.title optional) opens the panel from anywhere;
  // `mf-close-bell` closes it after a waiting row navigated away.
  useEffect(() => {
    const onOpen = (e: Event) => {
      const title = String((e as CustomEvent<{ title?: string } | null>).detail?.title || "");
      openRef.current();
      if (!title) return;
      // A session's title, or "run:<id>" for a group's own rows (a group can
      // hold several with no session — its budget, unresolved ticket lines —
      // so match on the run id, not on one row's key).
      const runId = title.startsWith("run:") ? title.slice(4) : "";
      const rows = attnRef.current;
      const ofRun = (r: (typeof rows)[number]) => !r.title && !!runId && r.waiting?.run?.id === runId;
      let at = rows.findIndex((r) => r.title === title || r.key === title);
      if (at < 0) at = rows.findIndex(ofRun);
      if (at >= NEEDS_SHOWN) setShowAll(true);
      setFlash(at >= 0 ? attnRef.current[at].key : title);
    };
    const onClose = () => setOpen(false);
    document.addEventListener("mf-open-bell", onOpen);
    document.addEventListener("mf-close-bell", onClose);
    return () => {
      document.removeEventListener("mf-open-bell", onOpen);
      document.removeEventListener("mf-close-bell", onClose);
    };
  }, []);

  // Scroll a requested row into view and flash it, once it has rendered.
  useEffect(() => {
    if (!open || !flash) return;
    const row = popRef.current?.querySelector<HTMLElement>('[data-needs="' + CSS.escape(flash) + '"]');
    row?.scrollIntoView({ block: "nearest" });
    const t = setTimeout(() => setFlash(null), 1600);
    return () => clearTimeout(t);
  }, [open, flash]);

  useEffect(() => {
    if (!open) setShowAll(false);
  }, [open]);

  const jump = (title: string) => {
    if (byTitle.has(title)) {
      selectSession(title);
      setOpen(false);
    }
  };

  const rect = btnRef.current?.getBoundingClientRect();
  const popStyle = rect
    ? { top: rect.bottom + 6 + "px", left: Math.max(8, Math.min(rect.left, window.innerWidth - 408)) + "px" }
    : undefined;

  const shownAttn = showAll ? attn : attn.slice(0, NEEDS_SHOWN);
  const runName = (id: string) => runs?.find((r) => r.id === id)?.name || "";

  return (
    <>
      <button
        id="notif-btn"
        ref={btnRef}
        className={
          "tb-item" + (attn.length > 0 ? " has-attn" : "") + (attn.length === 0 && unread > 0 ? " has-unread" : "")
        }
        type="button"
        title="Notifications — what needs you, and what happened while you were away"
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
          <div className="notif-pop" id="notif-pop" ref={popRef} style={popStyle}>
            <div className="notif-head">
              <span>Notifications</span>
              <button
                className="notif-settings"
                type="button"
                title="Notification settings — desktop notifications and which events notify"
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
                {shownAttn.map((it) => {
                  const flashing = flash !== null && (flash === it.title || flash === it.key);
                  if (it.waiting) {
                    // An approval or an escalation: the waiting row itself, with
                    // its preview and buttons — the click lives on those.
                    const w = it.waiting;
                    const runId = w.run?.id || "";
                    return (
                      <div
                        key={"w:" + it.key}
                        className={"attn-wait" + (flashing ? " flash" : "")}
                        data-needs={it.key}
                        data-attn={it.title || undefined}
                        data-run={runId || undefined}
                      >
                        <WaitingRow
                          w={w}
                          row={it.title ? byTitle.get(it.title) : undefined}
                          shown={windowName}
                          group={runId ? w.run?.name || runName(runId) : ""}
                          runInfo={runId ? runs?.find((r) => r.id === runId) : undefined}
                        />
                      </div>
                    );
                  }
                  const a = it.attn!;
                  // A prompt (p0) is answerable: every one carries the answer
                  // strip, which stands in for the snippet with the dialog's own
                  // question. Only a family's can redirect to a Thread.
                  const fam = a.p === 0 ? familyOf(it.title) : null;
                  return (
                    <div
                      key={it.key + a.reason}
                      className={"attn-item p" + a.p + (flashing ? " flash" : "")}
                      data-attn={it.title}
                      data-needs={it.key}
                      onClick={() => jump(it.title)}
                    >
                      <span className="attn-dot" />
                      <span className="attn-title">{windowName(it.title)}</span>
                      <span className="attn-reason">{a.reason}</span>
                      {fam?.parent && (
                        <span className="attn-lineage">{workerOf(fam.parent, displayName)}</span>
                      )}
                      {a.p === 0 ? (
                        <AnswerStrip
                          title={it.title}
                          activity="clarify"
                          variant="bell"
                          onOpen={() => jump(it.title)}
                          onRedirect={
                            fam
                              ? () => {
                                  setOpen(false);
                                  openThread(fam.parent || it.title, it.title);
                                }
                              : undefined
                          }
                        />
                      ) : (
                        !!a.snippet && (
                          <div className="attn-snippet">
                            “{typeof a.snippet === "string" ? a.snippet : JSON.stringify(a.snippet)}”
                          </div>
                        )
                      )}
                    </div>
                  );
                })}
                {attn.length > shownAttn.length && (
                  <button type="button" className="attn-more linklike" onClick={() => setShowAll(true)}>
                    +{attn.length - shownAttn.length} more
                  </button>
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
                    onClick={() => {
                      if (n.lead) {
                        // The plan to approve / the one PR to open live on
                        // the lead's Thread tab — the click goes there.
                        setOpen(false);
                        openThread(n.lead);
                      } else if (n.run && n.rule === "run_needs_you") {
                        // What it asks for is a row above, if it still waits:
                        // point at it rather than leaving the bell.
                        const row = attn.find((r) => r.waiting?.run?.id === n.run && (!n.session || r.title === n.session))
                          || attn.find((r) => r.waiting?.run?.id === n.run);
                        if (row) {
                          if (attn.indexOf(row) >= NEEDS_SHOWN) setShowAll(true);
                          setFlash(row.key);
                        } else {
                          // Answered already: show the group where it lives.
                          setOpen(false);
                          showGroup(n.run);
                        }
                      } else if (n.device) {
                        // Approve / Deny, and the roster, are in Settings → Devices.
                        setOpen(false);
                        useUi.getState().openDialogFor("settings", "devices");
                      } else if (n.run) {
                        // A group's row shows the group where it lives: its
                        // header on the rail (its ⋯ holds the summary, the
                        // queued lines, Pause and Cancel), else its lead's
                        // Thread tab (a split / one-for-all group).
                        setOpen(false);
                        showGroup(n.run);
                      } else jump(n.session);
                    }}
                  >
                    <span className="notif-sess">
                      {n.device && !n.session
                        ? "Devices"
                        : n.run && !n.session
                        ? runLookups.name(n.run) || "Group"
                        : (slotNumber(n.session) ? "[" + slotNumber(n.session) + "] " : "") +
                          (n.session ? windowName(n.session) : "—")}
                    </span>
                    <span className="notif-text">{n.text}</span>
                    <span className="notif-time">{relTime(n.ts)}</span>
                  </div>
                ))}
              </div>
            ) : attn.length === 0 ? (
              <div className="notif-empty muted">No notifications yet.</div>
            ) : null}
          </div>,
          document.body
        )}
    </>
  );
}
