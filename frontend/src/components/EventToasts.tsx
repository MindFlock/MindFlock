/** E2 — transition notifications + favicon/title badges (port of section 25).
 * Headless: renders nothing; subscribes to the client event bus, dedupes
 * toasts per session+state (30s), tracks the clarify set for the "● (n)"
 * title badge and the canvas favicon dot. */

import { useEffect } from "react";
import type { Instance } from "../api/types";
import {
  patchInstance,
  queryClient,
  refreshInstances,
  type EventEnvelope,
} from "../state/queries";
import { displayName, useUi } from "../state/store";
import { slotNumber } from "../lib/windowName";
import { fmtUsd } from "../lib/format";
import {
  dropActivity,
  effectiveActivity,
  followAutopilot,
  forceActivity,
} from "../lib/stage";
import { selectSession } from "../lib/sessionActions";
import { toast, type ToastOpts } from "../lib/toast";
import { messageToastText, type MessageEventData } from "../lib/agentMessages";
import { runNote } from "../lib/runs";
import { openThread } from "../lib/flockActions";
import { ruleOn, runLookups } from "../state/runs";
import { showGroup } from "../lib/showGroup";
import { PEER_SCREEN, peerEventNote } from "../lib/peer";
import { deviceEventNote } from "../lib/fleet";
import { desktopNotify, installDesktopNotifyClicks } from "../lib/desktopNotify";

const BASE_TITLE = document.title || "MindFlock";
const clarifyUnseen = new Set<string>(); // clarify sessions not yet looked at

function instances(): Instance[] {
  return queryClient.getQueryData<Instance[]>(["instances"]) || [];
}

function clarifySet(): Set<string> {
  return new Set(
    instances()
      .filter((i) => effectiveActivity(i) === "clarify")
      .map((i) => i.title)
  );
}

// --- Canvas favicon (background tabs can't see the title badge) --------------

let faviconLink: HTMLLinkElement | null = null;
const faviconLogo = new Image();
faviconLogo.src = "/logo.png";
faviconLogo.onload = () => {
  faviconState = null;
  updateTitleBadge();
};

function favicon(): HTMLLinkElement {
  if (faviconLink) return faviconLink;
  faviconLink = document.querySelector("link[rel='icon']");
  if (!faviconLink) {
    faviconLink = document.createElement("link");
    faviconLink.rel = "icon";
    document.head.appendChild(faviconLink);
  }
  return faviconLink;
}

let faviconState: string | null = null;

function updateFavicon(n: number) {
  // Black birds on white in dark mode (matches the desktop icon); inverted in
  // light mode. Theme is part of the cache key so a toggle redraws.
  const isLight = document.documentElement.classList.contains("light");
  const want = (n > 0 ? "dot" : "plain") + (isLight ? ":light" : ":dark");
  if (want === faviconState) return;
  faviconState = want;
  try {
    const tileBg = isLight ? "#0f1117" : "#ffffff";
    const birdFg = isLight ? "#ececf2" : "#111114";
    const c = document.createElement("canvas");
    c.width = c.height = 32;
    const g = c.getContext("2d")!;
    g.fillStyle = tileBg;
    if (g.roundRect) {
      g.beginPath();
      g.roundRect(0, 0, 32, 32, 7);
      g.fill();
    } else g.fillRect(0, 0, 32, 32);
    if (faviconLogo.complete && faviconLogo.naturalWidth) {
      // Tint the alpha-only logo on an offscreen canvas, then composite.
      const s = document.createElement("canvas");
      s.width = s.height = 32;
      const sg = s.getContext("2d")!;
      sg.drawImage(faviconLogo, 3, 3, 26, 26);
      sg.globalCompositeOperation = "source-in";
      sg.fillStyle = birdFg;
      sg.fillRect(0, 0, 32, 32);
      g.drawImage(s, 0, 0);
    } else {
      g.fillStyle = birdFg;
      g.font = "bold 20px system-ui, sans-serif";
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.fillText("M", 16, 17);
    }
    if (n > 0) {
      g.fillStyle = "#de613e";
      g.beginPath();
      g.arc(25, 7, 7, 0, Math.PI * 2);
      g.fill();
    }
    favicon().href = c.toDataURL("image/png");
  } catch {
    /* canvas unavailable — title badge still signals */
  }
}

export function updateTitleBadge() {
  const clarify = clarifySet();
  // Sessions that left clarify are handled by definition; re-arm them.
  for (const t of [...clarifyUnseen]) if (!clarify.has(t)) clarifyUnseen.delete(t);
  const n = clarifyUnseen.size;
  document.title = n ? "● (" + n + ") " + BASE_TITLE : BASE_TITLE;
  updateFavicon(n);
}

/** Theme toggle inverts the tab favicon — force a redraw now. */
export function redrawFavicon() {
  faviconState = null;
  updateTitleBadge();
}

function markClarify(title: string) {
  // Don't badge the session you're already looking at.
  if (document.hasFocus() && useUi.getState().focused === title) return;
  clarifyUnseen.add(title);
  updateTitleBadge();
}

function clearClarify(title?: string) {
  if (title) clarifyUnseen.delete(title);
  else clarifyUnseen.clear();
  updateTitleBadge();
}

/** Focusing a session marks its clarify as handled (selectSession hooks in). */
export function markSessionSeen(title: string) {
  if (clarifyUnseen.has(title)) clearClarify(title);
}

// Non-spammy toasts: at most one per session+state per 30s. The dedupe key is
// the raw title on purpose — renaming a session mid-storm must not reset its
// 30s window — while the message text names the session by displayName(), the
// same saved-name rule the bell, sidebar and panes use: a toast that says
// "untitled needs your input" about the tab you renamed to "rapisynth" makes
// the reader hunt for a session that doesn't appear to exist.
const notifyAt = new Map<string, number>();
function notifyOnce(session: string, state: string, msg: string, opts?: ToastOpts) {
  const key = session + "|" + state;
  const now = Date.now();
  if (now - (notifyAt.get(key) || 0) < 30000) return;
  notifyAt.set(key, now);
  toast(msg, opts);
}

// The name with its rail slot in front — "[3] sitecheck-bot7" — so a toast
// points at a row you can find at a glance. Same live resolver the OS
// notification and the bell use; blank slot (filtered out, tenth row on)
// degrades to the bare name.
function namedSlot(session: string): string {
  const n = slotNumber(session);
  return (n ? "[" + n + "] " : "") + displayName(session);
}

function instByTitle(title: string): Instance | null {
  return instances().find((i) => i.title === title) || null;
}

/** Jump to a session's Map — where every red-zone event is explained. A
 * store-level event (no session) opens the repo-wide Red zones dialog. */
function openMap(session: string) {
  if (!session || !instByTitle(session)) {
    useUi.getState().openDialogFor("red-zones");
    return;
  }
  selectSession(session);
  useUi.getState().setLastTab(session, "map");
}

/** The toast line for a red-zone event, or null for an unknown event. */
export function redZoneToast(env: EventEnvelope, name: string): string | null {
  const d = (env.data || {}) as { detail?: string; what?: string; paths?: string[] };
  const detail = String(d.detail || "").trim();
  switch (env.event) {
    case "session.red_zone_blocked":
      return "⛔ " + name + " — " + (detail || "a zone blocked an edit");
    case "session.red_zone_breached":
      return (
        "⛔ Zone breached on " +
        name +
        " — " +
        (detail || (d.paths && d.paths.length ? d.paths.slice(0, 3).join(", ") : "a protected file changed"))
      );
    case "session.red_zone_tampered": {
      const what = d.what === "hooks" ? "its hooks" : d.what === "store" ? "the zone list" : "the guard file";
      return env.session
        ? "⚠ Red-zone guard tampered on " + name + " (" + what + " changed outside MindFlock)" + (detail ? " — " + detail : "")
        : "⚠ Red zones changed outside MindFlock (" + what + ")";
    }
    default:
      return null;
  }
}

export function EventToasts() {
  // Keep the title badge fresh on every poll (clarify flips arrive both via
  // events and the 4s snapshot).
  const snapshot = queryClient.getQueryData<Instance[]>(["instances"]);
  useEffect(() => {
    updateTitleBadge();
  }, [snapshot]);

  // Focusing a session marks its clarify badge as handled (vanilla focusPane).
  const focused = useUi((s) => s.focused);
  useEffect(() => {
    if (focused) markSessionSeen(focused);
  }, [focused]);

  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    const isReplay = (env: EventEnvelope) =>
      !!(typeof ev.isReplay === "function" && ev.isReplay(env));
    const unsubs: Array<() => void> = [];

    // Bringing the tab back into focus counts as "seen".
    const onFocus = () => clearClarify();
    window.addEventListener("focus", onFocus);
    unsubs.push(() => window.removeEventListener("focus", onFocus));

    unsubs.push(
      ev.subscribe("session.activity_changed", (env) => {
        // Authoritative push — skip the 2-poll debounce.
        forceActivity(env.session, env.new || "idle");
        refreshInstances();
        updateTitleBadge();
        if (env.new === "clarify" && !isReplay(env)) {
          markClarify(env.session);
          const inst = instByTitle(env.session);
          const snip = inst?.last_turn ? " — “" + inst.last_turn + "”" : "";
          notifyOnce(env.session, "clarify", namedSlot(env.session) + " needs your input" + snip, {
            onClick: () => selectSession(env.session),
          });
        }
      })
    );
    unsubs.push(
      ev.subscribe("session.setup_finished", (env) => {
        if (isReplay(env) || env.new === "ok") return;
        notifyOnce(env.session, "setupfail", "worktree setup failed on " + namedSlot(env.session) + " — prompts held", {
          onClick: () => selectSession(env.session),
        });
      })
    );
    unsubs.push(
      ev.subscribe("session.check_finished", (env) => {
        if (isReplay(env) || env.new === "ok") return;
        notifyOnce(env.session, "checkfail", "checks failed on " + namedSlot(env.session), {
          onClick: () => selectSession(env.session),
        });
      })
    );
    unsubs.push(
      // Autopilot steps reached the UI only on the next 4s poll, so "pushing" could
      // be over before it appeared. Patch the cache from the socket instead — same
      // shape and same before-the-replay-guard reasoning as stage_changed below.
      ev.subscribe("session.autopilot_changed", (env) => {
        const d = (env.data || {}) as Record<string, unknown>;
        const depth = String(d.depth || "");
        patchInstance(env.session, {
          autopilot: depth
            ? {
                depth,
                state: String(env.new || d.state || "running"),
                step: String(d.step || ""),
                reason: String(d.reason || ""),
                note: String(d.note || ""),
                source: String(d.source || "session"),
                item: String(d.item || ""),
                skipped: Array.isArray(d.skipped) ? (d.skipped as string[]) : [],
              }
            : null,
        });
        if (isReplay(env)) return;
        // Follow the run to whatever it is doing (terminal on commit, the PR when
        // it opens). Shared with the per-poll reconcile in App.tsx via one guard,
        // so it happens exactly once per step whichever path notices first.
        followAutopilot(
          {
            title: env.session,
            autopilot: {
              depth: String(d.depth || ""),
              state: String(env.new || d.state || "running"),
              step: String(d.step || ""),
              reason: String(d.reason || ""),
              note: String(d.note || ""),
              url: String(d.url || ""),
              source: String(d.source || "session"),
              item: String(d.item || ""),
            },
          },
          { live: true }
        );
        if (String(env.new || "") === "halted")
          notifyOnce(env.session, "ftstop", "fast-track stopped on " + namedSlot(env.session), {
            onClick: () => selectSession(env.session),
          });
      })
    );
    unsubs.push(
      ev.subscribe("session.stage_changed", (env) => {
        // Patch the cache BEFORE the replay guard: a replayed stage is still the
        // truth for the cache (only the toast must be suppressed). This is the
        // 0-round-trip half of the freshness fix — the socket is already open and
        // this subscriber already exists; the new stage was simply discarded
        // after toasting, leaving the UI to wait for its next 4s poll.
        if (env.new) patchInstance(env.session, { stage: String(env.new) as Instance["stage"] });
        if (isReplay(env)) return; // toast-only subscriber — skip stale history
        const title = env.session;
        if (env.new === "interrupt") {
          notifyOnce(title, "interrupt", "pre-commit failed on " + displayName(title), {
            onClick: () => selectSession(title),
          });
        }
        // Both PR toasts moved to session.pr_state_changed below. The stage
        // ladder leaves and re-enters "pr" on every edit/commit/push cycle of a
        // PR that never moved, and notifyOnce only dedupes for 30s — so keying
        // them here re-toasted "PR merged or closed" and then "PR open" for the
        // rest of a review session.
      })
    );
    unsubs.push(
      ev.subscribe("session.pr_state_changed", (env) => {
        if (isReplay(env)) return;
        const title = env.session;
        const url = String((env.data as { url?: string } | undefined)?.url || "");
        if (env.new === "OPEN") {
          notifyOnce(title, "pr", "PR open for " + displayName(title), {
            onClick: () => {
              const inst = instByTitle(title);
              const href = inst?.pr_url || url;
              if (href) window.open(href, "_blank");
              else selectSession(title);
            },
          });
        } else if (env.old === "OPEN") {
          notifyOnce(title, "merged", displayName(title) + ": PR merged or closed ✓", {
            onClick: () => selectSession(title),
          });
        }
      })
    );
    // Red zones: a block, a breach, or someone disarming the guard. The rail
    // chip reads the row summary, so refresh it now instead of on the next poll.
    for (const name of ["session.red_zone_blocked", "session.red_zone_breached", "session.red_zone_tampered"]) {
      unsubs.push(
        ev.subscribe(name, (env) => {
          refreshInstances();
          if (isReplay(env)) return;
          const msg = redZoneToast(env, env.session ? namedSlot(env.session) : "");
          if (!msg) return;
          notifyOnce(env.session || "*", name, msg, {
            onClick: () => openMap(env.session),
            duration: name === "session.red_zone_blocked" ? 6000 : 9000,
          });
        })
      );
    }
    // Agent-to-agent messages (MindFlock MCP). Kept quiet on purpose: an
    // orchestrator fanning one instruction out to five workers is ONE toast,
    // not five — throttled per sender (30s, notifyOnce) — while each worker's
    // report is its own line (keyed per worker → parent pair), because "w3
    // reported: failed" is exactly the thing you'd otherwise miss. Never for a
    // replayed backlog: a reconnect must not re-announce old traffic.
    unsubs.push(
      ev.subscribe("session.message", (env) => {
        if (isReplay(env) || !env.session) return;
        const d = (env.data || {}) as MessageEventData;
        const from = String(d.from || "");
        const isResult = d.kind === "result";
        notifyOnce(
          isResult ? env.session : from || "*external",
          isResult ? "result:" + from : "message",
          messageToastText(env.session, d, displayName),
          {
            onClick: () => {
              if (instByTitle(env.session)) selectSession(env.session);
            },
            duration: isResult ? 8000 : 5000,
          }
        );
      })
    );
    // Ship lanes: a group that needs you, or has finished. At most ONE run
    // toast per 30s across every group (one notifyOnce key) — a group's lines
    // tend to finish together, and the bell keeps every row anyway. Same rule
    // switches as the bell; never for a replayed backlog; a line that merely
    // shipped is bell-only (its rule is off for push/desktop by default).
    for (const name of ["run.needs_you", "run.finished"]) {
      unsubs.push(
        ev.subscribe(name, (env) => {
          if (isReplay(env)) return;
          const n = runNote(env.event, env.data, runLookups);
          if (!n || !ruleOn(n.rule)) return;
          notifyOnce("*run", "run", n.text, {
            // A plan / the one PR: their click is on the lead's Thread tab.
            // Anything else that needs you waits in the bell; a finished
            // group is shown where it lives — its header on the rail, whose
            // ⋯ copies the summary, else its lead's Thread tab.
            onClick: () => {
              if (n.lead) openThread(n.lead);
              else if (env.event === "run.needs_you")
                // A line's session, or the group's own row ("run:<id>") when
                // the escalation names no session.
                document.dispatchEvent(
                  new CustomEvent("mf-open-bell", {
                    detail: { title: String(env.data?.title || "") || (n.run ? "run:" + n.run : "") },
                  })
                );
              else showGroup(n.run);
            },
            duration: 8000,
          });
        })
      );
    }
    // Your devices: someone asking to join is the one that needs you (the
    // click opens Settings → Devices, where Approve is — after comparing the
    // code both screens show); a join is news; settings sync pausing itself
    // stops all syncing until it's answered there. Never for a replayed
    // backlog: a request from an hour ago has expired, and the bell keeps the
    // record.
    // The desktop app also raises an OS notification for the ones that need
    // you while it's minimized (lib/desktopNotify): a join request, someone
    // arriving on a peer link, an update.
    unsubs.push(installDesktopNotifyClicks());
    unsubs.push(
      ev.subscribe("update.available", (env) => {
        if (!isReplay(env)) desktopNotify(env.event, env.data);
      })
    );
    for (const name of ["device.join_requested", "device.joined", "settings.sync_paused"]) {
      unsubs.push(
        ev.subscribe(name, (env) => {
          if (isReplay(env)) return;
          desktopNotify(env.event, env.data);
          const n = deviceEventNote(env.event, env.data);
          if (!n?.toast) return;
          notifyOnce("*device:" + String(env.data?.device || ""), name, n.toast, {
            onClick: () => useUi.getState().openDialogFor("settings", "devices"),
            duration: 6000,
          });
        })
      );
    }
    // Another person (peer links): someone you invited arrived (compare the
    // safety number), unlinked you, left a message where no shared session
    // takes it, or your relay moved. The click opens Work with someone.
    for (const name of ["peer.link_added", "peer.link_removed", "peer.message", "peer.relay_changed"]) {
      unsubs.push(
        ev.subscribe(name, (env) => {
          if (isReplay(env)) return;
          desktopNotify(env.event, env.data);
          const n = peerEventNote(env.event, env.data);
          if (!n?.toast) return;
          notifyOnce("*peer:" + String(env.data?.link_id || ""), name, n.toast, {
            onClick: () => useUi.getState().openDialogFor("settings", PEER_SCREEN),
            duration: 7000,
          });
        })
      );
    }
    unsubs.push(
      ev.subscribe("session.deleted", (env) => {
        dropActivity(env.session);
        clearClarify(env.session);
      })
    );
    unsubs.push(
      ev.subscribe("session.budget_exceeded", (env) => {
        if (isReplay(env)) return;
        const d = (env.data || {}) as { cost?: number; budget?: number };
        notifyOnce(
          env.session,
          "budget",
          namedSlot(env.session) + " exceeded its budget (" + fmtUsd(d.cost || 0) + " of " + fmtUsd(d.budget || 0) + ")",
          { onClick: () => selectSession(env.session), duration: 8000 }
        );
      })
    );
    return () => unsubs.forEach((u) => u());
  }, []);

  return null;
}
