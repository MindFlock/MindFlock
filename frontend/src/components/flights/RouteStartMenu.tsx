/** The route-start menu (lib/routeStart): one small menu at the click — never
 * a dialog — for the three choices a start can need:
 *
 *  - a saved route that an earlier run on this session / branch / ticket left
 *    part way: "Carries on at Push — where it stopped 2h ago (…)", then
 *    Carry on at Push (focused: Enter takes the default), Start from the
 *    beginning, Start at another stop…;
 *  - "Start at…": the route's own stops, numbered, each with its kind's icon;
 *  - "Fly again from another stop…": an ended route's stops.
 *
 * Mounted once (App); opened from anywhere through `useRouteStartMenu`. ↑ / ↓
 * move, Enter picks, Esc or a press outside closes it. It also keeps where
 * the person last pressed (`lastPress`), so a menu opened from a ⋯ item
 * appears by it. */

import { useEffect, useRef, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { flyAgain } from "../../lib/flightsApi";
import type { StepKindDef } from "../../lib/flights";
import { choiceLine, lastPress, reachedIndex, resumeSentence, stopChoices, useRouteStartMenu, type RouteStartOpen } from "../../lib/routeStart";
import { useFlightKinds } from "../../state/queries";
import { StopIcon } from "./Bird";
import "./StopMenu.css";
import "./RouteStartMenu.css";

/** One line of the menu. `note` is a sentence, not a choice; `kind` draws the
 * stop's icon; `primary` is the default (focused when it opens). The keys
 * `pick` (open the stops) and `back` (back to the carry-on line) are the
 * menu's own; any other pick calls `run`. */
export interface RouteMenuLine {
  key: string;
  label: string;
  title?: string;
  note?: boolean;
  kind?: string;
  primary?: boolean;
  run?(): void;
}

/** What the menu lists for `open` (pure but for the `run` callbacks). */
export function routeMenuLines(open: RouteStartOpen, kinds?: readonly StepKindDef[] | null, now?: number): RouteMenuLine[] {
  if (open.mode === "again") {
    const f = open.flight;
    const k = reachedIndex(f);
    const at = k < f.steps.length ? k : 0;
    return [
      { key: "note", label: "Fly “" + f.plan_name + "” again from…", note: true },
      ...stopChoices(f.steps, kinds).map((c, i) => ({
        key: "stop:" + c.id,
        label: choiceLine(c) + (i === 0 ? " — the beginning" : "") + (i === k ? " — where it stopped" : ""),
        title: i === k ? "Fly it again from where it stopped" : "Fly it again from “" + c.title + "”",
        kind: c.kind,
        primary: i === at,
        run: () => void flyAgain(f.id, i === k ? "" : c.id),
      })),
    ];
  }
  const resume = open.check?.resume || null;
  if (open.view === "resume" && resume) {
    return [
      { key: "note", label: resumeSentence(resume, now), note: true },
      { key: "resume", label: "Carry on at " + resume.title, title: "Start “" + open.plan.name + "” at “" + resume.title + "” — the stops before it are skipped", primary: true, run: () => open.go(resume.stop) },
      { key: "fresh", label: "Start from the beginning", title: "Start “" + open.plan.name + "” at its first stop", run: () => open.go("") },
      { key: "pick", label: "Start at another stop…", title: "Pick the stop it starts at" },
    ];
  }
  const want = resume?.stop || open.plan.steps[0]?.id;
  return [
    { key: "note", label: "Start “" + open.plan.name + "” at…", note: true },
    ...stopChoices(open.plan.steps, kinds).map((c, i) => ({
      key: "stop:" + c.id,
      label: choiceLine(c) + (resume && c.id === resume.stop ? " — where it stopped" : ""),
      title: i === 0 ? "Start at its first stop" : "Start at “" + c.title + "” — the stops before it are skipped",
      kind: c.kind,
      primary: c.id === want,
      run: () => open.go(c.id),
    })),
    ...(open.back ? [{ key: "back", label: "← Back", title: "Back to where it carries on" }] : []),
  ];
}

export function RouteStartMenu() {
  const open = useRouteStartMenu((s) => s.open);
  const set = useRouteStartMenu((s) => s.set);
  const kinds = useFlightKinds(!!open).data;
  const ref = useRef<HTMLDivElement>(null);

  // Where the person last pressed: a menu opened from a ⋯ item opens there.
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      lastPress.x = e.clientX;
      lastPress.y = e.clientY;
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, []);

  const view = open ? (open.mode === "start" ? open.view : "again") : "";
  useEffect(() => {
    if (!open) return;
    const all = ref.current?.querySelectorAll<HTMLButtonElement>("button[role=menuitem]");
    (ref.current?.querySelector<HTMLButtonElement>("button.is-default") || all?.[0])?.focus();
    const onDown = (e: Event) => {
      if (ref.current && !ref.current.contains(e.target as Node)) set(null);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      set(null);
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open, view, set]);

  if (!open || typeof document === "undefined") return null;
  const lines = routeMenuLines(open, kinds);

  const pick = (l: RouteMenuLine) => {
    if (open.mode === "start" && l.key === "pick") return set({ ...open, view: "pick", back: true });
    if (open.mode === "start" && l.key === "back") return set({ ...open, view: "resume", back: false });
    set(null);
    l.run?.();
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const all = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button[role=menuitem]") || [])];
    if (!all.length) return;
    const i = all.indexOf(document.activeElement as HTMLButtonElement);
    const go = (j: number) => {
      e.preventDefault();
      all[(j + all.length) % all.length].focus();
    };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i < 0 ? -1 : i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(-1);
    else if (e.key === "Tab") {
      e.preventDefault();
      set(null);
    }
  };
  const label = open.mode === "again" ? "Fly “" + open.flight.plan_name + "” again from…" : "Where “" + open.plan.name + "” starts";

  return createPortal(
    <div className="fl-routestart-host" style={{ position: "fixed", left: 0, top: 0, width: 0, height: 0, zIndex: 85 }}>
      <div
        ref={ref}
        className="fl-stopmenu fl-routestart"
        role="menu"
        aria-label={label}
        style={{ left: open.x, top: open.y }}
        data-no-drag
        onKeyDown={onKey}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => e.stopPropagation()}
        onContextMenu={(e) => e.preventDefault()}
      >
        {lines.map((l) =>
          l.note ? (
            <div key={l.key} className="fl-stopmenu-note fl-routestart-note" role="menuitem" aria-disabled="true">
              {l.label}
            </div>
          ) : (
            <button
              key={l.key}
              type="button"
              role="menuitem"
              className={"fl-stopmenu-item fl-routestart-item" + (l.primary ? " is-default" : "")}
              title={l.title}
              onClick={() => pick(l)}
            >
              {l.kind && (
                <span className="fl-routestart-icon" aria-hidden="true">
                  <StopIcon kind={l.kind === "you" ? "perch" : l.kind} size={13} />
                </span>
              )}
              <span>{l.label}</span>
            </button>
          )
        )}
      </div>
    </div>,
    document.body
  );
}
