/** Code tree — colours. Everything comes from the app's CSS tokens (--bg,
 * --panel, --text, --muted, --accent, --red, --green, --gold …) and from the
 * tree's own custom properties (--ct-*, defined in CodeMapTab.css from those
 * tokens, with light-theme overrides in theme-light.css): sky, soil, bark,
 * foliage lightness, label halo, pill backgrounds. Read once per theme change
 * (draw.ts caches by `key`). */

export interface Pal {
  blob: string;
  blobHi: string;
  leafA: string;
  leafB: string;
  doc: string;
  fold: string;
  label: string;
  cv: [string, string, string];
  cvHi: string;
  cvSh: string;
  bark?: string;
}

export interface Palette {
  key: string;
  light: boolean;
  font: string;
  bg: string;
  panel: string;
  panel2: string;
  border: string;
  text: string;
  muted: string;
  accent: string;
  red: string;
  green: string;
  gold: string;
  /** the calm silhouette: the unchanged tree recedes (low saturation, compressed lightness) */
  PAL: Pal[];
  /** the tree's full colours: an only-here territory keeps them */
  PALV: Pal[];
  /** lit wood: the branch path from the trunk to a change */
  litBark: string;
  /** the calm label colour of an unchanged folder */
  calmLabel: string;
  /** text on a solid zone sign (red keep-out / green only-here) */
  signKeepText: string;
  signOnlyText: string;
  DUSK: Pal & { bark: string };
  KEEP: Pal;
  ROOTPAL: Pal;
  PILEPAL: { leafA: string; leafB: string; doc: string };
  BARK: { crown: string; crownHi: string; root: string; rootHi: string; rootDusk: string };
  duskLabel: string;
  duskLeafLabel: string;
  rootLabel: string;
  pileLabel: string;
  keepText: string;
  onlyText: string;
  leafLabel: string;
  sky: [string, string];
  soil: [string, string];
  glow: string;
  glow0: string;
  groundLine: string;
  mass: string;
  halo: string;
  pill: string;
  /** the hairline around a label lozenge (neutral: labels never carry a bird's colour as an outline) */
  pillEdge: string;
  pillText: string;
  keepBg: string;
  onlyBg: string;
  keepWrap: string;
  onlyWrap: string;
  hatch: string;
  hatchDim: string;
  badgeBg: string;
  sel: string;
  selHalo: string;
  mark: string;
  hover: string;
  sep: string;
  bud: string;
  budGhost: string;
  bonkBg: string;
  bonkX: string;
  nestBowl: string;
  nestTwig: string;
  nestRim: string;
  editStroke: string;
  glyphStroke: string;
  vine: string;
  miniBg: string;
  miniSoil: string;
  miniDim: string;
  miniView: string;
  miniRoot: string;
}

const HUES = [146, 104, 172, 124, 88, 158, 116, 186, 98, 136, 166, 80, 130, 178, 110, 152];

/** The bird colours, after this session's (the accent): colour-blind-safe
 * secondaries that keep apart from the accent and from gold. */
export const BIRD_FALLBACK = ["#ffa24d", "#56b4e9", "#ee5d8f", "#e6d75a", "#4fd1a5", "#c58cff"];
export const BIRD_GLYPHS = ["●", "▲", "■", "◆", "★", "✚", "⬟"];
/** A helper's glyph is its parent's shape HOLLOW plus its index ("○1", "△2"): a solid "●4" after a folder's name
 * is always a change count, never a helper. */
const HOLLOW: Record<string, string> = { "●": "○", "▲": "△", "■": "□", "◆": "◇", "★": "☆", "✚": "✛", "⬟": "⬠" };
export function helperGlyph(parent: string, index: number): string {
  return (HOLLOW[parent] || "○") + index;
}
/** The solid shape a glyph stands for (a helper's hollow glyph + index → its parent's shape). */
export function glyphShape(glyph: string): string {
  const g = glyph.replace(/\d+$/, "");
  for (const k in HOLLOW) if (HOLLOW[k] === g) return k;
  return g;
}

/** Custom properties come back as written (var()s substituted, color-mix()
 * not evaluated): resolve such a colour to rgb() through a probe element, so
 * the canvas — which takes any CSS colour, but not every engine evaluates
 * color-mix() in fillStyle — gets a plain one. */
let PROBE: HTMLElement | null = null;
function resolveColor(v: string): string {
  if (!/color-mix|var\(|light-dark/.test(v) || typeof document === "undefined") return v;
  try {
    if (!PROBE || !PROBE.isConnected) {
      PROBE = document.createElement("span");
      PROBE.style.display = "none";
      PROBE.setAttribute("aria-hidden", "true");
      document.body.appendChild(PROBE);
    }
    PROBE.style.color = "";
    PROBE.style.color = v;
    const out = getComputedStyle(PROBE).color;
    return out || v;
  } catch {
    return v;
  }
}
function readVar(cs: CSSStyleDeclaration | null, name: string, dflt: string): string {
  if (!cs) return dflt;
  const v = cs.getPropertyValue(name).trim();
  return v ? resolveColor(v) : dflt;
}
function num(cs: CSSStyleDeclaration | null, name: string, dflt: number): number {
  const v = parseFloat(readVar(cs, name, ""));
  return Number.isFinite(v) ? v : dflt;
}

/** Build the palette from computed styles (`el` = the tree's host element, so
 * the tree's custom properties resolve; null = the dark defaults, for tests). */
export function readPalette(el: Element | null): Palette {
  const cs = el && typeof getComputedStyle !== "undefined" ? getComputedStyle(el) : null;
  const light = typeof document !== "undefined" && document.documentElement.classList.contains("light");
  const v = (n: string, d: string) => readVar(cs, n, d);
  // foliage lightness and saturation knobs: the light theme lifts the
  // greens off a pale sky instead of the dark one
  const A = num(cs, "--ct-fol-a", 0);
  const L = num(cs, "--ct-fol-l", 1);
  const Sat = num(cs, "--ct-fol-s", 1);
  const LL = num(cs, "--ct-label-l", 72);
  const cl = (x: number) => Math.max(4, Math.min(92, x));
  const hsl = (h: number, s: number, l: number) => `hsl(${h},${Math.min(100, Math.round(s * Sat))}%,${cl(A + l * L).toFixed(1)}%)`;
  const PAL: Pal[] = [];
  const PALV: Pal[] = [];
  // calm: saturation down to a third and lightness pulled toward the canopy's middle, so the unchanged tree is a
  // quiet silhouette and only the changes (bird colours, lit wood) carry contrast
  const CS = num(cs, "--ct-calm-s", 0.2),
    CK = num(cs, "--ct-calm-k", 0.3),
    CM = num(cs, "--ct-calm-mid", 23);
  const calm = (h: number, s: number, l: number) => hsl(h, s * CS, CM + (l - CM) * CK);
  for (let i = 0; i < 40; i++) {
    const h = HUES[i % HUES.length];
    for (const [arr, f] of [
      [PAL, calm],
      [PALV, hsl],
    ] as Array<[Pal[], typeof hsl]>)
      arr.push({
        blob: f(h, 26, 21),
        blobHi: f(h, 30, 27),
        leafA: f(h, 34, 46),
        leafB: f(h + 8, 29, 39),
        doc: f(h - 20, 9, 42),
        fold: f(h, 20, 26),
        label: arr === PAL ? v("--ct-calm-label", "#8a90a2") : `hsl(${h},30%,${LL}%)`,
        cv: [f(h, 30, 33), f(h + 9, 27, 29), f(h - 7, 33, 38)],
        cvHi: f(h + 4, 36, 47),
        cvSh: f(h, 28, 13),
      });
  }
  const dusk = v("--ct-dusk", "#23262d");
  const DUSK = {
    blob: v("--ct-dusk-blob", "#191c22"),
    blobHi: v("--ct-dusk-blob-hi", "#1c1f26"),
    leafA: v("--ct-dusk-leaf", "#2b2f37"),
    leafB: v("--ct-dusk-leaf-b", "#272a31"),
    doc: v("--ct-dusk-doc", "#262930"),
    bark: v("--ct-dusk-bark", "#2a2827"),
    fold: v("--ct-dusk-fold", "#1f2228"),
    label: v("--ct-dusk-label", "#a3a9b8"),
    cv: [dusk, v("--ct-dusk-b", "#212329"), v("--ct-dusk-c", "#25282f")] as [string, string, string],
    cvHi: v("--ct-dusk-hi", "#2b2e35"),
    cvSh: v("--ct-dusk-sh", "#131519"),
  };
  const KEEP: Pal = {
    blob: hsl(10, 30, 18),
    blobHi: hsl(10, 34, 22),
    leafA: hsl(10, 48, 46),
    leafB: hsl(14, 40, 38),
    doc: hsl(10, 24, 38),
    fold: hsl(10, 30, 24),
    label: v("--ct-keep-text", "#ffb7a3"),
    cv: [hsl(10, 40, 33), hsl(14, 36, 29), hsl(8, 42, 37)],
    cvHi: hsl(12, 48, 46),
    cvSh: hsl(10, 30, 13),
  };
  const RA = num(cs, "--ct-root-a", 0);
  const RL = num(cs, "--ct-root-l", 1);
  const rh = (h: number, s: number, l: number) => `hsl(${h},${s}%,${Math.max(4, Math.min(92, RA + l * RL)).toFixed(1)}%)`;
  const ROOTPAL: Pal = {
    blob: rh(28, 24, 10.5),
    blobHi: rh(28, 22, 12),
    leafA: rh(34, 20, 40),
    leafB: rh(30, 16, 34),
    doc: rh(30, 10, 30),
    fold: rh(28, 20, 15),
    label: v("--ct-root-label", "hsl(34,30%,70%)"),
    cv: [rh(30, 18, 24), rh(26, 16, 21), rh(33, 20, 27)],
    cvHi: rh(34, 20, 33),
    cvSh: rh(28, 20, 8),
  };
  const PILEPAL = { leafA: rh(30, 30, 40), leafB: rh(22, 25, 33), doc: rh(30, 30, 40) };
  const red = v("--red", "#de613e"),
    green = v("--green", "#51bd73"),
    gold = v("--gold", "#ffd700");
  const raw = (n: string, d: string) => (cs ? cs.getPropertyValue(n).trim() || d : d);
  const glowRgb = raw("--ct-glow", "90, 150, 110");
  const hatchRgb = raw("--ct-hatch", "255, 150, 120");
  const selRgb = raw("--ct-select", "255, 255, 255");
  const P: Palette = {
    key: "",
    light,
    font: v("--ct-font", "system-ui, -apple-system, Segoe UI, sans-serif"),
    bg: v("--bg", "#0f1117"),
    panel: v("--panel", "#171a23"),
    panel2: v("--panel-2", "#1e222e"),
    border: v("--border", "#2a2f3c"),
    text: v("--text", "#d7dae3"),
    muted: v("--muted", "#8a90a2"),
    accent: v("--accent", "#7d56f4"),
    red,
    green,
    gold,
    PAL,
    PALV,
    litBark: v("--ct-lit-bark", "hsl(32,22%,62%)"),
    calmLabel: v("--ct-calm-label", "#8a90a2"),
    signKeepText: v("--ct-sign-keep-text", "#fff"),
    signOnlyText: v("--ct-sign-only-text", "#062611"),
    DUSK,
    KEEP,
    ROOTPAL,
    PILEPAL,
    BARK: {
      crown: v("--ct-calm-bark", v("--ct-bark", "hsl(28,14%,37%)")),
      crownHi: v("--ct-bark-hi", "hsl(30,14%,46%)"),
      root: v("--ct-root", "hsl(27,22%,30%)"),
      rootHi: v("--ct-root-hi", "hsl(28,26%,42%)"),
      rootDusk: v("--ct-root-dusk", "#1f1c1a"),
    },
    duskLabel: DUSK.label,
    duskLeafLabel: v("--ct-dusk-leaf-label", "#7c8291"),
    rootLabel: ROOTPAL.label,
    pileLabel: v("--ct-pile-label", "hsl(32,32%,66%)"),
    keepText: v("--ct-keep-text", "#ffb7a3"),
    onlyText: v("--ct-only-text", "#a8f0c0"),
    leafLabel: v("--ct-leaf-label", "#c9cdd6"),
    sky: [v("--ct-sky-top", "#0d0f15"), v("--ct-sky-bottom", "#131722")],
    soil: [v("--ct-soil-top", "#16120e"), v("--ct-soil-bottom", "#0c0a08")],
    glow: `rgba(${glowRgb},0.035)`,
    glow0: `rgba(${glowRgb},0)`,
    groundLine: v("--ct-ground-line", "hsl(30,14%,24%)"),
    mass: v("--ct-calm-mass", v("--ct-mass", "hsl(142,26%,20%)")),
    halo: v("--ct-halo", "rgba(12,14,19,0.92)"),
    pill: v("--ct-pill", "rgba(15,17,23,0.9)"),
    pillEdge: v("--ct-pill-edge", "rgba(255,255,255,0.08)"),
    pillText: v("--ct-pill-text", "#eef0f5"),
    keepBg: v("--ct-keep-bg", "rgba(60,20,14,0.95)"),
    onlyBg: v("--ct-only-bg", "rgba(16,46,28,0.95)"),
    keepWrap: v("--ct-keep-wrap", "rgba(40,10,6,0.95)"),
    onlyWrap: v("--ct-only-wrap", "rgba(6,30,14,0.95)"),
    hatch: `rgba(${hatchRgb},0.5)`,
    hatchDim: `rgba(${hatchRgb},0.32)`,
    badgeBg: v("--ct-badge-bg", "rgba(24,22,10,0.94)"),
    sel: `rgb(${selRgb})`,
    selHalo: `rgba(${selRgb},0.35)`,
    mark: `rgba(${selRgb},0.7)`,
    hover: v("--ct-hover", "rgba(170,176,190,0.8)"),
    sep: v("--ct-sep", "#5a6070"),
    bud: v("--ct-bud", "rgba(255,228,240,0.95)"),
    budGhost: v("--ct-bud-ghost", "rgba(255,240,246,0.92)"),
    bonkBg: v("--ct-bonk-bg", "rgba(40,12,8,0.9)"),
    bonkX: v("--ct-bonk-x", "#ff9b80"),
    nestBowl: v("--ct-nest", "hsl(33,32%,26%)"),
    nestTwig: v("--ct-nest-twig", "hsl(34,30%,44%)"),
    nestRim: v("--ct-nest-rim", "hsl(34,26%,58%)"),
    editStroke: `rgba(${selRgb},0.85)`,
    glyphStroke: v("--ct-halo", "rgba(12,14,19,0.9)"),
    vine: "rgba(255,215,0,0.22)",
    miniBg: v("--ct-mini-bg", "#12151d"),
    miniSoil: v("--ct-mini-soil", "#17130f"),
    miniDim: v("--ct-mini-dim", "rgba(10,12,16,0.62)"),
    miniView: v("--ct-mini-view", "rgba(215,218,227,0.8)"),
    miniRoot: v("--ct-mini-root", "hsl(30,22%,22%)"),
  };
  P.key = [light, P.bg, P.accent, P.text, P.red, P.green, P.gold, P.sky.join(), P.BARK.crown, A, L, Sat, P.halo].join("|");
  return P;
}

/** Bird colours: this session takes the accent; the others take the
 * secondaries, skipping any too close in hue to the accent. */
export function birdColours(accent: string, n: number, extra: string[] = BIRD_FALLBACK): string[] {
  const hue = (c: string): number | null => {
    const m = /^#([0-9a-f]{6})$/i.exec(c.trim());
    if (!m) return null;
    const x = parseInt(m[1], 16);
    const r = (x >> 16) / 255,
      g = ((x >> 8) & 255) / 255,
      b = (x & 255) / 255;
    const mx = Math.max(r, g, b),
      mn = Math.min(r, g, b);
    if (mx === mn) return null;
    const d = mx - mn;
    const h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return (h * 60 + 360) % 360;
  };
  const ha = hue(accent);
  const pool = extra.filter((c) => {
    const h = hue(c);
    if (ha === null || h === null) return true;
    const d = Math.abs(h - ha);
    return Math.min(d, 360 - d) > 28;
  });
  const out = [accent];
  for (let i = 1; i < n; i++) out.push(pool[(i - 1) % Math.max(1, pool.length)] || extra[(i - 1) % extra.length]);
  return out;
}
