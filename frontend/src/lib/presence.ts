/** Owner, driver and viewers of a session (backend/web/core/ownership.py,
 * docs/people.md): who has it open, and who holds its keyboard.
 *
 * Pure helpers for the rail's presence dots and the pane header's one drive
 * control, plus the person query they compare against. A person alone with
 * their sessions sees NEITHER: both appear only once someone else is on the
 * session (watching or driving) — the solo case is every session, and a dot
 * of your own initials on every row would say nothing. */

import { useQuery } from "@tanstack/react-query";
import { api, instApi } from "../api/client";
import type { Instance, Person, Viewer } from "../api/types";

/** Dots drawn on a rail row; the rest fold into "+N". */
export const MAX_DOTS = 3;

/** This browser's person (GET /api/person) — what "you" means on a row. All of
 * one person's devices share the id, so it holds for remote rows too. */
export function useMe(): string {
  const q = useQuery({
    queryKey: ["person"],
    queryFn: () => api<Person>("/api/person"),
    staleTime: Infinity,
    retry: false,
  });
  return q.data?.id || "";
}

/** "Alice Smith" → "AS", "alice" → "A", "" with device "laptop" → "L". */
export function initials(name: string, device = ""): string {
  const words = (name || device || "?").trim().split(/[\s._-]+/).filter(Boolean);
  const letters = words.slice(0, 2).map((w) => w[0]!.toUpperCase());
  return letters.join("") || "?";
}

/** Who a viewer is in a sentence: their name, "You", their device, or
 * "Someone" (another person's name doesn't travel between servers). */
export function whoLabel(pid: string, v: Viewer | undefined, me: string): string {
  if (v?.name) return v.name;
  if (pid && pid === me) return "You";
  return v?.device || "Someone";
}

export interface PresenceDot {
  person: string;
  initials: string;
  /** "Alice (laptop) · driving" */
  tip: string;
  driving: boolean;
  typing: boolean;
}

/** Whether anyone besides `me` is on the session — the only time presence is
 * worth drawing. */
export function othersPresent(inst: Pick<Instance, "viewers" | "driver">, me: string): boolean {
  const others = Object.keys(inst.viewers || {}).filter((p) => p && p !== me);
  return others.length > 0 || !!(inst.driver && inst.driver !== me);
}

/** The rail row's dots: the driver first, then viewers by when they arrived,
 * at most {@link MAX_DOTS} (`more` counts the rest). Empty unless someone
 * besides `me` is there. */
export function presenceDots(
  inst: Pick<Instance, "viewers" | "driver" | "driver_name">,
  me: string,
): { dots: PresenceDot[]; more: number } {
  if (!othersPresent(inst, me)) return { dots: [], more: 0 };
  const viewers = { ...(inst.viewers || {}) };
  const driver = inst.driver || "";
  if (driver && !viewers[driver]) {
    // Driving without a terminal open (a send with "take"): still a person.
    viewers[driver] = {
      name: driver === me ? "" : inst.driver_name || "",
      device: "",
      since: 0,
      typing: false,
    };
  }
  const order = Object.keys(viewers)
    .filter(Boolean)
    .sort((a, b) =>
      a === driver ? -1 : b === driver ? 1 : (viewers[a]!.since || 0) - (viewers[b]!.since || 0),
    );
  const dots = order.map((pid) => {
    const v = viewers[pid]!;
    const who = whoLabel(pid, v, me);
    const where = v.device ? ` (${v.device})` : "";
    const doing = pid === driver ? "driving" : v.typing ? "typing" : "watching";
    return {
      person: pid,
      initials: pid === me && !v.name ? initials("You") : initials(v.name, v.device),
      tip: `${who}${where} · ${doing}`,
      driving: pid === driver,
      typing: !!v.typing,
    };
  });
  return { dots: dots.slice(0, MAX_DOTS), more: Math.max(0, dots.length - MAX_DOTS) };
}

export interface DriveControl {
  /** The control's words: "Driving" | "Watching — take control" | "Take control". */
  label: string;
  tip: string;
  driving: boolean;
  /** What a click does. */
  action: "take" | "release";
}

/** The pane header's one control, or null when there is nothing to say (you
 * are alone on the session, or the server predates ownership). */
export function driveControl(
  inst: Pick<Instance, "viewers" | "driver" | "driver_name">,
  me: string,
): DriveControl | null {
  if (inst.driver === undefined || !othersPresent(inst, me)) return null;
  const driver = inst.driver || "";
  if (driver && driver === me) {
    const others = Object.entries(inst.viewers || {})
      .filter(([p]) => p && p !== me)
      .map(([p, v]) => whoLabel(p, v, me));
    return {
      label: "Driving",
      tip:
        "You hold the keyboard" +
        (others.length ? ` — ${others.join(", ")} watching` : "") +
        ". Click to let go.",
      driving: true,
      action: "release",
    };
  }
  if (driver) {
    const name = inst.driver_name || whoLabel(driver, inst.viewers?.[driver], me);
    return {
      label: "Watching — take control",
      tip: `${name} is driving. Take control to type here (they are told).`,
      driving: false,
      action: "take",
    };
  }
  return {
    label: "Take control",
    tip: "Nobody holds the keyboard. Take it to type here.",
    driving: false,
    action: "take",
  };
}

/** POST /api/instances/{title}/driver — remote rows ride the gateway's
 * forward of the namespaced title like every per-session route. */
export function setDriver(title: string, action: "take" | "release") {
  return instApi<Partial<Instance>>(title, "/driver", {
    json: action === "take" ? { take: true } : { release: true },
  });
}

/** The toast a `session.driver_changed` event earns, or null: only the
 * person who LOST the keyboard to someone else hears about it ("Bob took
 * control of 3 · api — you're watching now"). Your own takes, a lapse for
 * idleness and a release are silent — nobody else is typing. */
export function lostKeyboardNote(
  env: { old: string | null; new: string | null; data: Record<string, unknown> },
  me: string,
  sessionName: string,
): string | null {
  if (!me || env.old !== me || !env.new || env.new === me) return null;
  const who = typeof env.data?.name === "string" && env.data.name ? env.data.name : "Someone";
  return `${who} took control of ${sessionName} — you're watching now`;
}
