/** Settings → Appearance (partial 102 + section 17's appearance wiring):
 * surface + accent swatch pickers. Server (ui.accent / ui.surface) is the
 * source of truth; localStorage (cs_accent / cs_surface) is the pre-paint
 * cache the inline <head> script reads. */

import { useState } from "react";
import { api } from "../../../api/client";
import { rethemeAll } from "../../../lib/terminals";
import type { ScreenProps } from "../SettingsDialog";

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
  // Cosmetic — a failed save just stays per-browser.
  api("/api/settings", { json: { ui: { [dim.field]: name } } }).catch(() => {});
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
        One bird per set, painted the way the bird actually is: most give the top bar, the
        sidebar and the window a hue each — Scarlet Macaw is a cobalt bar over an emerald
        sidebar over a scarlet window. Swallow, Raven and Heron are the quiet ones. Every set
        has its own light-mode palette, so the moon toggle in the top bar keeps working inside
        all of them. Synced to all your devices.
      </p>
      <div id="surface-swatches" className="accent-swatches">
        {SURFACES.map(surfaceSwatch)}
      </div>
      <p className="set-hint">
        Accents recolor highlights, focus rings, buttons and the terminal cursor — mix freely
        with any theme set.
      </p>
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
      <p className="set-hint">Dark / light mode itself is the moon toggle in the top bar.</p>
    </>
  );
}
