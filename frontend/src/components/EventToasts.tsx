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
import { useUi } from "../state/store";
import { fmtUsd } from "../lib/format";
import {
  dropActivity,
  effectiveActivity,
  followAutopilot,
  forceActivity,
} from "../lib/stage";
import { selectSession } from "../lib/sessionActions";
import { toast, type ToastOpts } from "../lib/toast";

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

// Non-spammy toasts: at most one per session+state per 30s.
const notifyAt = new Map<string, number>();
function notifyOnce(session: string, state: string, msg: string, opts?: ToastOpts) {
  const key = session + "|" + state;
  const now = Date.now();
  if (now - (notifyAt.get(key) || 0) < 30000) return;
  notifyAt.set(key, now);
  toast(msg, opts);
}

function instByTitle(title: string): Instance | null {
  return instances().find((i) => i.title === title) || null;
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
          notifyOnce(env.session, "clarify", env.session + " needs your input" + snip, {
            onClick: () => selectSession(env.session),
          });
        }
      })
    );
    unsubs.push(
      ev.subscribe("session.setup_finished", (env) => {
        if (isReplay(env) || env.new === "ok") return;
        notifyOnce(env.session, "setupfail", "worktree setup failed on " + env.session + " — prompts held", {
          onClick: () => selectSession(env.session),
        });
      })
    );
    unsubs.push(
      ev.subscribe("session.check_finished", (env) => {
        if (isReplay(env) || env.new === "ok") return;
        notifyOnce(env.session, "checkfail", "checks failed on " + env.session, {
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
          notifyOnce(env.session, "ftstop", "fast-track stopped on " + env.session, {
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
        if (env.new === "pr") {
          notifyOnce(title, "pr", "PR open for " + title, {
            onClick: () => {
              const inst = instByTitle(title);
              if (inst?.pr_url) window.open(inst.pr_url, "_blank");
              else selectSession(title);
            },
          });
        } else if (env.new === "interrupt") {
          notifyOnce(title, "interrupt", "pre-commit failed on " + title, {
            onClick: () => selectSession(title),
          });
        } else if (env.old === "pr") {
          // F4: the server never emits "merged" — a merged/closed PR moves the
          // stage OFF "pr" (same detection as the notify addon).
          notifyOnce(title, "merged", title + ": PR merged or closed ✓", {
            onClick: () => selectSession(title),
          });
        }
      })
    );
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
          env.session + " exceeded its budget (" + fmtUsd(d.cost || 0) + " of " + fmtUsd(d.budget || 0) + ")",
          { onClick: () => selectSession(env.session), duration: 8000 }
        );
      })
    );
    return () => unsubs.forEach((u) => u());
  }, []);

  return null;
}
