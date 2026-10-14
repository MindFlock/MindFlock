/** Settings → Appearance (partial 102 + section 17's appearance wiring):
 * surface + accent swatch pickers, then the comfort switches — Reduce motion,
 * Take a break, Idle flock — which moved here from General (they are about
 * how the app looks and feels, not about the work). Server (ui.accent / ui.surface) is the
 * source of truth; localStorage (cs_accent / cs_surface) is the pre-paint
 * cache the inline <head> script reads. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import {
  BREAK_MAX_MINUTES,
  BREAK_MIN_MINUTES,
  clampBreakMinutes,
  clampIdleMinutes,
  IDLE_MAX_MINUTES,
  IDLE_MIN_MINUTES,
} from "../../../lib/breakTimer";
import { rethemeAll } from "../../../lib/terminals";
import { toast } from "../../../lib/toast";
import { useUi } from "../../../state/store";
import type { ScreenProps } from "../SettingsDialog";
import { LearnMore } from "../settingsMode";

/* The aviary, in the order theme.css defines it: the three quiet neutrals, then
   round the color wheel by the hue the window wears. Where a name exists in both
   dimensions the key is shared ("cardinal" is the red accent AND the red
   surface), so the two pickers read as one palette; Swallow (default) and Raven
   are surface-only.

   Swatch tones mirror the layout — top bar, sidebar, window — so a chip is a
   little picture of the set. Neutrals pass two tones (background / panel)
   instead of three; swatchGradient bands whatever it's given. */
type Surface = [key: string, label: string, ...swatch: string[]];

const SURFACES: Surface[] = [
  ["", "Swallow (default)", "#0f1117", "#1e222e"],
  ["raven", "Raven", "#000000", "#141419"],
  ["heron", "Heron", "#16181b", "#292d33"],
  ["cardinal", "Cardinal", "#d51322", "#16060a", "#3d060d"],
  ["macaw", "Scarlet Macaw", "#1554e0", "#06773a", "#43060a"],
  ["pheasant", "Golden Pheasant", "#f5b800", "#05361f", "#3f070c"],
  ["oriole", "Oriole", "#ff7a12", "#140c03", "#421d03"],
  ["lorikeet", "Rainbow Lorikeet", "#4b1fd6", "#256e0a", "#4f1c05"],
  ["roller", "Lilac-breasted Roller", "#9b6ef0", "#00736b", "#3a2412"],
  ["goldfinch", "Goldfinch", "#f2e00d", "#131300", "#33350d"],
  ["toucan", "Toco Toucan", "#ffb01f", "#08080a", "#181820"],
  ["quetzal", "Quetzal", "#00a35c", "#4a0512", "#033524"],
  ["greenjay", "Green Jay", "#1440d6", "#d6b800", "#103a0e"],
  ["gouldian", "Gouldian Finch", "#00c2b2", "#52157a", "#0e3a19"],
  ["mallard", "Mallard", "#0d9c62", "#5e330f", "#22262e"],
  ["kingfisher", "Kingfisher", "#00a8d6", "#5e2404", "#06323f"],
  ["peacock", "Peacock", "#0a9cb8", "#141f8f", "#05383f"],
  ["bluejay", "Bluejay", "#1266e6", "#0a0e1c", "#071a44"],
  ["bunting", "Indigo Bunting", "#3b2ff0", "#08061f", "#150f52"],
  ["violetear", "Violetear", "#7c2ff0", "#0e0420", "#220c40"],
  ["mandarin", "Mandarin Duck", "#12a85e", "#f2820f", "#3a1445"],
  ["flamingo", "Flamingo", "#ff2d8a", "#180110", "#420527"],
];

/** Equal-width diagonal bands, one per tone in the set's swatch. */
function swatchGradient(tones: string[]): string {
  const step = 100 / tones.length;
  const stops = tones.map(
    (c, i) => `${c} ${(i * step).toFixed(1)}% ${((i + 1) * step).toFixed(1)}%`
  );
  return `linear-gradient(135deg,${stops.join(",")})`;
}

// Gold and silver get a metallic sheen in the swatch dot; --accent itself stays
// a flat color (a gradient can't be used as a border/cursor color).
const ACCENTS: Array<[string, string, string]> = [
  ["", "Violetear (default)", "#7d56f4"],
  ["cardinal", "Cardinal", "#e5484d"],
  ["oriole", "Oriole", "#f07b3c"],
  ["goldfinch", "Goldfinch", "#f0cf28"],
  ["quetzal", "Quetzal", "#44b556"],
  ["bluejay", "Bluejay", "#3d8bfd"],
  ["bunting", "Indigo Bunting", "#4f46e5"],
  ["flamingo", "Flamingo", "#f2559b"],
  ["pheasant", "Golden Pheasant", "linear-gradient(135deg,#f0d78a,#b8860b)"],
  ["heron", "Heron", "linear-gradient(135deg,#e2e7ef,#8d95a3)"],
];

interface Dim {
  field: "accent" | "surface";
  attr: string;
  lsKey: string;
}

const DIMS: Record<string, Dim> = {
  accent: { field: "accent", attr: "data-accent", lsKey: "cs_accent" },
  surface: { field: "surface", attr: "data-surface", lsKey: "cs_surface" },
};

/** (Re)apply the cached appearance attributes on <html> — called once at
 * startup from main.tsx. */
export function applyStoredAppearance() {
  for (const dim of Object.values(DIMS)) {
    let saved = "";
    try {
      saved = localStorage.getItem(dim.lsKey) || "";
    } catch {
      /* storage unavailable */
    }
    if (saved) document.documentElement.setAttribute(dim.attr, saved);
    else document.documentElement.removeAttribute(dim.attr);
  }
}

/** The accent this browser has cached ("" = default). */
export function storedAccent(): string {
  try {
    return localStorage.getItem(DIMS.accent.lsKey) || "";
  } catch {
    return "";
  }
}

/** Take an accent chosen elsewhere — `ui.accent` synced from another of your
 * devices (lib/prefsSync.ts). Same paint as picking a swatch here, minus the
 * POST: the server already holds it, and writing it back would re-stamp it as
 * this device's own edit. */
export function adoptAccent(name: string) {
  const dim = DIMS.accent;
  if (name) document.documentElement.setAttribute(dim.attr, name);
  else document.documentElement.removeAttribute(dim.attr);
  try {
    if (name) localStorage.setItem(dim.lsKey, name);
    else localStorage.removeItem(dim.lsKey);
  } catch {
    /* storage unavailable */
  }
  rethemeAll();
}

function current(dim: Dim): string {
  return document.documentElement.getAttribute(dim.attr) || "";
}

function apply(dim: Dim, name: string) {
  if (name) document.documentElement.setAttribute(dim.attr, name);
  else document.documentElement.removeAttribute(dim.attr);
  try {
    if (name) localStorage.setItem(dim.lsKey, name);
    else localStorage.removeItem(dim.lsKey);
  } catch {
    /* storage unavailable */
  }
  // Cosmetic: a failed save stays per-browser — and says so, rather than
  // the next sync quietly putting the old one back.
  api("/api/settings", { json: { ui: { [dim.field]: name } } }).catch((e) =>
    toast(
      "Couldn't save the " + dim.field + " for your other devices — " +
        ((e as Error)?.message || "the server refused it"),
      { duration: 8000 }
    )
  );
  rethemeAll(); // open terminals pick up the new canvas/cursor colors
}

export function Appearance(_: ScreenProps) {
  const [accent, setAccent] = useState(() => current(DIMS.accent));
  const [surface, setSurface] = useState(() => current(DIMS.surface));

  const surfaceSwatch = ([name, label, ...tones]: Surface) => (
    <button
      key={name || "default"}
      type="button"
      className={"accent-swatch surface-swatch" + (surface === name ? " active" : "")}
      data-surface-choice={name}
      onClick={() => {
        setSurface(name);
        apply(DIMS.surface, name);
      }}
    >
      <span className="sw-dot" style={{ background: swatchGradient(tones) }} />
      {label}
    </button>
  );

  return (
    <>
      <h3 className="set-section-title">Appearance</h3>
      <p className="set-hint">
        Light or dark is the moon button in the top bar. Every theme below has both.
      </p>
      <h3 className="set-section-title">Theme</h3>
      <div id="surface-swatches" className="accent-swatches">
        {SURFACES.map(surfaceSwatch)}
      </div>
      <h3 className="set-section-title">Accent</h3>
      <p className="set-hint">Highlights, buttons and the terminal cursor. Mix with any theme.</p>
      <div id="accent-swatches" className="accent-swatches">
        {ACCENTS.map(([name, label, color]) => (
          <button
            key={name || "default"}
            type="button"
            className={"accent-swatch" + (accent === name ? " active" : "")}
            data-accent-choice={name}
            onClick={() => {
              setAccent(name);
              apply(DIMS.accent, name);
            }}
          >
            <span className="sw-dot" style={{ background: color }} />
            {label}
          </button>
        ))}
      </div>
      <h3 className="set-section-title">Comfort</h3>
      <ReduceMotionRow />
      <TakeABreakRow />
      <IdleFlockRow />
    </>
  );
}

/** Reduce motion: while an agent is running, cover its terminal with a static
 * "running" panel instead of the live (flickering) output — easier on the eyes.
 * Off by default; the cover lifts on interaction (see Pane's RunningCover). */
function ReduceMotionRow() {
  const reduceMotion = useUi((s) => s.reduceMotion);
  const setReduceMotion = useUi((s) => s.setReduceMotion);
  return (
    <div className="set-row set-switch-row">
      <span className="notif-rule-text">
        <span className="set-label">Reduce motion</span>
        <span className="set-hint notif-rule-desc">
          Hide a running agent's scrolling output behind a still panel.
        </span>
        <LearnMore>
          Clicking, scrolling or typing in that window brings the live output back; the panel
          returns after 10 seconds with no input. Only the Agent tab is covered.
        </LearnMore>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={reduceMotion}
          onChange={(e) => {
            setReduceMotion(e.target.checked);
            toast(e.target.checked ? "Reduce motion on" : "Reduce motion off");
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** Take a break: a reminder on a timer, with the flock from mindflock.ai flying
 * over your grid. Snooze pushes it back five minutes; "Resume Working" restarts
 * the full interval. Off by default — an app that interrupts you uninvited is a
 * worse app.
 *
 * The row's whole description is the sentence it configures, with the interval
 * editable in place. Shown whether or not the switch is on, because it is what
 * tells you what the switch does. */
function TakeABreakRow() {
  const on = useUi((s) => s.breakReminder);
  const every = useUi((s) => s.breakEveryMin);
  const setOn = useUi((s) => s.setBreakReminder);
  const setEvery = useUi((s) => s.setBreakEveryMin);
  // Local text state so the field can be empty mid-edit; the store only ever
  // sees a clamped whole number (typing "9" on the way to "90" would otherwise
  // be clamped to the 5-minute floor and eat the keystroke).
  const [draft, setDraft] = useState(String(every));
  useEffect(() => setDraft(String(every)), [every]);

  const commit = (raw: string) => {
    const next = clampBreakMinutes(raw === "" ? every : raw);
    setEvery(next);
    setDraft(String(next));
    if (on) toast("Break reminder every " + next + " min");
  };

  return (
    <div className="set-row set-switch-row">
      <span className="notif-rule-text">
        <span className="set-label">Take a break</span>
        <span className="set-hint notif-rule-desc break-every">
          Reminder to take a break every{" "}
          <input
            type="number"
            aria-label="Minutes between break reminders"
            min={BREAK_MIN_MINUTES}
            max={BREAK_MAX_MINUTES}
            step={5}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => commit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />{" "}
          minutes.
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            setOn(e.target.checked);
            toast(
              e.target.checked
                ? "Break reminder on — every " + every + " min"
                : "Break reminder off"
            );
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** The idle flock: birds over the grid once nobody has touched this window for
 * a while. On by default, and the one animation in the app that is safe to
 * leave on — it can only appear when you are not there to be interrupted. The
 * delay is the same sentence-with-a-field shape as the break row above.
 *
 * "Touched" means a click, a keystroke, a scroll or a tap in THIS window; a
 * drifting mouse and a streaming agent are not you (see breaks/useIdle). */
function IdleFlockRow() {
  const on = useUi((s) => s.idleFlock);
  const after = useUi((s) => s.idleFlockAfterMin);
  const setOn = useUi((s) => s.setIdleFlock);
  const setAfter = useUi((s) => s.setIdleFlockAfterMin);
  // Local text state so the field can be empty mid-edit — same reason as the
  // break interval: typing "1" on the way to "15" must not commit a 1.
  const [draft, setDraft] = useState(String(after));
  useEffect(() => setDraft(String(after)), [after]);

  const commit = (raw: string) => {
    const next = clampIdleMinutes(raw === "" ? after : raw);
    setAfter(next);
    setDraft(String(next));
    if (on) toast("Idle flock after " + next + " min");
  };

  return (
    <div className="set-row set-switch-row">
      <span className="notif-rule-text">
        <span className="set-label">Idle flock</span>
        <span className="set-hint notif-rule-desc break-every">
          Fly the flock over your grid after{" "}
          <input
            type="number"
            aria-label="Minutes idle before the flock appears"
            min={IDLE_MIN_MINUTES}
            max={IDLE_MAX_MINUTES}
            step={5}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => commit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />{" "}
          minutes of no input. The first touch sends them home.
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            setOn(e.target.checked);
            toast(
              e.target.checked ? "Idle flock on — after " + after + " min" : "Idle flock off"
            );
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

