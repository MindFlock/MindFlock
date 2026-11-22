/** The terminal's 16 ANSI colours, one set per mode.
 *
 * xterm's built-in palette is tuned for black: on a light canvas its yellow
 * (#e5e510) was 1.3:1 and its cyan 2.7:1, so an agent's coloured output (diff
 * stats, test results, warnings) all but vanished in every light theme. Each
 * colour here holds 4.5:1 against the terminal background of every surface set
 * in its mode (themeContrast.test.ts walks them); in light mode that includes
 * "white" and "bright white", which programs print as emphasis — they are
 * greys there, not white. Dark mode's "black" is exempt: it is meant to sit
 * near the background.
 *
 * Kept free of imports so the contrast test (node environment) can read it
 * without pulling xterm in. mobile.js carries a copy of DARK (the phone has no
 * light mode); the test pins the two together. */

export interface AnsiPalette {
  black: string;
  red: string;
  green: string;
  yellow: string;
  blue: string;
  magenta: string;
  cyan: string;
  white: string;
  brightBlack: string;
  brightRed: string;
  brightGreen: string;
  brightYellow: string;
  brightBlue: string;
  brightMagenta: string;
  brightCyan: string;
  brightWhite: string;
}

export const ANSI_DARK: AnsiPalette = {
  black: "#484f58",
  red: "#ff7b72",
  green: "#56d364",
  yellow: "#e3b341",
  blue: "#58a6ff",
  magenta: "#d2a8ff",
  cyan: "#39c5cf",
  white: "#c9d1d9",
  brightBlack: "#8b949e",
  brightRed: "#ffa198",
  brightGreen: "#7ee787",
  brightYellow: "#f2cc60",
  brightBlue: "#a5d6ff",
  brightMagenta: "#e2c5ff",
  brightCyan: "#7fe3eb",
  brightWhite: "#f0f6fc",
};

export const ANSI_LIGHT: AnsiPalette = {
  black: "#24292f",
  red: "#c3212c",
  green: "#116329",
  yellow: "#6f4b00",
  blue: "#0a5fcc",
  magenta: "#7340c8",
  cyan: "#136b72",
  white: "#59616b",
  brightBlack: "#57606a",
  brightRed: "#a40e26",
  brightGreen: "#1a7136",
  brightYellow: "#633c01",
  brightBlue: "#0550ae",
  brightMagenta: "#6639ba",
  brightCyan: "#0f5b61",
  brightWhite: "#424a53",
};

/** The selection wash, drawn OVER the canvas (xterm blends it): 40% of a grey,
 * so selected text keeps 4.5:1 on every set's terminal background. */
export const SELECTION_DARK = "#3a415466";
export const SELECTION_LIGHT = "#b9c0d466";

/** The palette for a mode. */
export function ansiPalette(light: boolean): AnsiPalette {
  return light ? ANSI_LIGHT : ANSI_DARK;
}
