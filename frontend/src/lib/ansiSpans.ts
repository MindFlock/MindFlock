/** A terminal screen with SGR colour codes → styled runs, for the shared-
 * session viewer (components/PeerSessionViewer.tsx). The server already
 * stripped everything but `ESC [ … m` (backend/peer/screen.py sanitize);
 * this turns those into inline styles React renders as plain <span>s — no
 * HTML is ever built from the text, so nothing in it can run.
 *
 * Covers what agent CLIs actually emit: reset, bold, dim, italic, underline,
 * reverse, the 8/16 colours, 256-colour (38;5;n) and truecolour (38;2;r;g;b),
 * for foreground and background. Anything else is ignored. */

export interface AnsiStyle {
  color?: string;
  background?: string;
  bold?: boolean;
  dim?: boolean;
  italic?: boolean;
  underline?: boolean;
  reverse?: boolean;
}

export interface AnsiRun {
  text: string;
  style: AnsiStyle;
}

const BASE16 = [
  "#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
  "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff",
];

/** xterm's 256-colour palette entry `n` as a CSS colour. */
export function xterm256(n: number): string {
  if (n < 16) return BASE16[n];
  if (n < 232) {
    const i = n - 16;
    const step = (v: number) => (v === 0 ? 0 : 55 + v * 40);
    return `rgb(${step(Math.floor(i / 36))},${step(Math.floor(i / 6) % 6)},${step(i % 6)})`;
  }
  const g = 8 + (n - 232) * 10;
  return `rgb(${g},${g},${g})`;
}

function apply(style: AnsiStyle, params: number[]): AnsiStyle {
  const s: AnsiStyle = { ...style };
  if (!params.length) params = [0];
  for (let i = 0; i < params.length; i++) {
    const p = params[i];
    if (p === 0) {
      for (const k of Object.keys(s)) delete (s as Record<string, unknown>)[k];
    } else if (p === 1) s.bold = true;
    else if (p === 2) s.dim = true;
    else if (p === 3) s.italic = true;
    else if (p === 4) s.underline = true;
    else if (p === 7) s.reverse = true;
    else if (p === 22) s.bold = s.dim = undefined;
    else if (p === 23) s.italic = undefined;
    else if (p === 24) s.underline = undefined;
    else if (p === 27) s.reverse = undefined;
    else if (p >= 30 && p <= 37) s.color = BASE16[p - 30];
    else if (p >= 90 && p <= 97) s.color = BASE16[p - 90 + 8];
    else if (p === 39) s.color = undefined;
    else if (p >= 40 && p <= 47) s.background = BASE16[p - 40];
    else if (p >= 100 && p <= 107) s.background = BASE16[p - 100 + 8];
    else if (p === 49) s.background = undefined;
    else if (p === 38 || p === 48) {
      const key = p === 38 ? "color" : "background";
      if (params[i + 1] === 5 && params[i + 2] !== undefined) {
        s[key] = xterm256(Math.max(0, Math.min(255, params[i + 2])));
        i += 2;
      } else if (params[i + 1] === 2 && params[i + 4] !== undefined) {
        const [r, g, b] = params.slice(i + 2, i + 5).map((v) => Math.max(0, Math.min(255, v)));
        s[key] = `rgb(${r},${g},${b})`;
        i += 4;
      }
    }
  }
  return s;
}

/** Split `text` into lines of styled runs. Style carries across lines, as it
 * does in a terminal. */
export function ansiLines(text: string): AnsiRun[][] {
  const lines: AnsiRun[][] = [];
  let style: AnsiStyle = {};
  for (const raw of text.split("\n")) {
    const runs: AnsiRun[] = [];
    const re = /\x1b\[([0-9;:]*)m/g;
    let pos = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(raw))) {
      if (m.index > pos) runs.push({ text: raw.slice(pos, m.index), style });
      const params = m[1] ? m[1].split(/[;:]/).map((x) => Number(x) || 0) : [];
      style = apply(style, params);
      pos = re.lastIndex;
    }
    if (pos < raw.length) runs.push({ text: raw.slice(pos), style });
    lines.push(runs);
  }
  // A capture ends with blank rows below the prompt; they are noise here.
  while (lines.length && lines[lines.length - 1].every((r) => !r.text.trim())) lines.pop();
  return lines;
}

/** The inline CSS for a run (reverse swaps fg/bg, defaulting to the theme). */
export function runCss(s: AnsiStyle): Record<string, string> {
  const css: Record<string, string> = {};
  let fg = s.color;
  let bg = s.background;
  if (s.reverse) {
    [fg, bg] = [bg || "var(--bg)", fg || "var(--text)"];
  }
  if (fg) css.color = fg;
  if (bg) css.background = bg;
  if (s.bold) css.fontWeight = "700";
  if (s.dim) css.opacity = "0.7";
  if (s.italic) css.fontStyle = "italic";
  if (s.underline) css.textDecoration = "underline";
  return css;
}
