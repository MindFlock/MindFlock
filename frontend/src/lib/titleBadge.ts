/** The tab's "● (n)" title badge and the canvas favicon dot: how many
 * sessions came to need you (lib/sessionState `inBell`, behind each state's
 * Settings → Notifications switch) that you haven't looked at since. Split
 * out of EventToasts, which tells it when an event changed the answer; the
 * hook keeps it fresh on every poll and counts focus as "seen". */

import { useEffect } from "react";
import type { Instance } from "../api/types";
import { queryClient } from "../state/queries";
import { useUi } from "../state/store";
import { ruleOn } from "../state/runs";
import { deriveSessionState, notifyRuleFor } from "./sessionState";

const BASE_TITLE = document.title || "MindFlock";
// Sessions that need you (lib/sessionState `inBell`, behind their Settings →
// Notifications switch) that nobody has looked at since they started to.
const attentionUnseen = new Set<string>();
// What the badge counted last time, so a session can be told apart as NEWLY
// needing you (→ unseen) from one that has needed you all along.
let attentionWas = new Set<string>();

function instances(): Instance[] {
  return queryClient.getQueryData<Instance[]>(["instances"]) || [];
}

/** The sessions whose state is in the bell AND whose notify switch is on:
 * the bell shows every `inBell` state regardless (it is the source of truth);
 * the toast and this badge obey the switch. */
export function badgeWanted(rows: Instance[] = instances()): Set<string> {
  const out = new Set<string>();
  for (const i of rows) {
    const ss = deriveSessionState(i);
    if (ss.inBell && ruleOn(notifyRuleFor(ss))) out.add(i.title);
  }
  return out;
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
  const want = badgeWanted();
  // A session that stopped needing you is handled by definition; re-arm it.
  for (const t of [...attentionUnseen]) if (!want.has(t)) attentionUnseen.delete(t);
  // One that STARTED needing you is unseen — unless you are looking at it.
  const looking = document.hasFocus() ? useUi.getState().focused : "";
  for (const t of want) if (!attentionWas.has(t) && t !== looking) attentionUnseen.add(t);
  attentionWas = want;
  const n = attentionUnseen.size;
  document.title = n ? "● (" + n + ") " + BASE_TITLE : BASE_TITLE;
  updateFavicon(n);
}

/** Theme toggle inverts the tab favicon — force a redraw now. */
export function redrawFavicon() {
  faviconState = null;
  updateTitleBadge();
}

/** Count a session (or, with no title, every one) as seen. */
export function clearAttention(title?: string) {
  if (title) attentionUnseen.delete(title);
  else attentionUnseen.clear();
  updateTitleBadge();
}

/** Focusing a session marks its attention as handled (selectSession hooks in). */
export function markSessionSeen(title: string) {
  if (attentionUnseen.has(title)) clearAttention(title);
}

/** Mount once (EventToasts does): repaint on every instances poll, mark the
 * focused session seen, and count bringing the tab back as seeing them all. */
export function useTitleBadge() {
  // Clarify flips arrive both via events and the 4s snapshot.
  const snapshot = queryClient.getQueryData<Instance[]>(["instances"]);
  useEffect(() => {
    updateTitleBadge();
  }, [snapshot]);

  // Focusing a session marks its badge as handled (vanilla focusPane).
  const focused = useUi((s) => s.focused);
  useEffect(() => {
    if (focused) markSessionSeen(focused);
  }, [focused]);

  useEffect(() => {
    const onFocus = () => clearAttention();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);
}
