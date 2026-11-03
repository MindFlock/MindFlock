/** The Sky's keyboard shortcuts and gestures, as the `?` sheet lists them
 * (components/flights/SkyShortcuts, docs/flights.md "Round 17": task
 * `zoom-shortcuts`). One list, read off SkyView's key handler and the
 * round 12–17 "As built" sections; SkyView passes it as the sheet's `groups`.
 * A new key in the sky gets its row here, in the group a newcomer would look
 * in first.
 *
 * Keys are written as pressed, one cap each: ["Ctrl", "Z"], ["Shift",
 * "drag"], ["?"]. `withPlatform` turns Ctrl into ⌘ and Alt into ⌥ on a Mac;
 * `filterShortcuts` is the sheet's filter (by key or by words). */

import type { ShortcutGroup, ShortcutItem } from "../components/flights/SkyShortcuts";

export const SKY_SHORTCUT_GROUPS: ShortcutGroup[] = [
  {
    title: "Move around",
    items: [
      { keys: ["Space", "drag"], what: "Pan from anywhere" },
      { keys: ["Wheel"], what: "Pan (two fingers on a trackpad)" },
      { keys: ["Ctrl", "Wheel"], what: "Zoom about the pointer (or pinch)" },
      { keys: ["Arrows"], what: "Pan a step (with Shift, a bigger one)" },
      { keys: ["+"], what: "Zoom in" },
      { keys: ["−"], what: "Zoom out" },
      { keys: ["Shift", "1"], what: "Fit all the lines" },
      { keys: ["0"], what: "Fit all the lines" },
      { keys: ["Shift", "2"], what: "Fit the selection" },
      { keys: ["1"], what: "Fit the focused line" },
      { keys: ["Shift", "0"], what: "Zoom to 100 %" },
      { keys: ["H"], what: "Click tool: a drag pans" },
    ],
  },
  {
    title: "Select",
    items: [
      { keys: ["V"], what: "Drag tool: a drag on empty sky selects" },
      { keys: ["drag"], what: "Select what the box touches (on empty sky)" },
      { keys: ["Shift", "drag"], what: "Add to the selection" },
      { keys: ["Shift", "click"], what: "Add or take out one line or box" },
      { keys: ["Ctrl", "A"], what: "Select every line" },
      { keys: ["F"], what: "Focus: dim everything but the selection" },
      { keys: ["Esc"], what: "Clear the selection, or leave nothing focused" },
    ],
  },
  {
    title: "Edit stops",
    items: [
      { keys: ["double-click"], what: "Edit a box (or ✎)" },
      { keys: ["Enter"], what: "Open the focused line's box" },
      { keys: ["right-click"], what: "A box's menu" },
      { keys: ["Delete"], what: "Remove the selection or the focused box" },
      { keys: ["Backspace"], what: "Remove, like Delete" },
      { keys: ["Ctrl", "Z"], what: "Undo the last removal" },
      { keys: ["Ctrl", "Shift", "Z"], what: "Redo" },
      { keys: ["Esc"], what: "Close the open box" },
    ],
  },
  {
    title: "Lines and objects",
    items: [
      { keys: ["drag"], what: "Move a line by its bar (the selected ones go together)" },
      { keys: ["Alt", "drag"], what: "Move without snapping to the others" },
      { keys: ["T"], what: "Tidy: every line back in its place, every window minimised" },
      { keys: ["Ctrl", "N"], what: "A new session block (Alt+N in a browser tab)" },
      { keys: ["double-click"], what: "On empty sky: a new session block there" },
      { keys: ["Ctrl", "Y"], what: "Switch between the grid and the sky" },
    ],
  },
  {
    title: "The palette",
    items: [
      { keys: ["Tab"], what: "Open the palette with its filter (again: dock it)" },
      { keys: ["click"], what: "Place a block on the sky" },
      { keys: ["drag"], what: "Drop a block onto a line to add it there" },
      { keys: ["Esc"], what: "Dock a floating palette" },
    ],
  },
  {
    title: "Help",
    items: [
      { keys: ["?"], what: "These shortcuts" },
      { keys: ["Esc"], what: "Close a card, a menu or this sheet" },
    ],
  },
];

const MAC_CAPS: Record<string, string> = { Ctrl: "⌘", Alt: "⌥" };

/** The groups as a Mac shows them: Ctrl → ⌘, Alt → ⌥ (the keys the sky
 * reads as `metaKey` / `altKey` there). Elsewhere, the groups unchanged. */
export function withPlatform(groups: readonly ShortcutGroup[], isMac: boolean): ShortcutGroup[] {
  if (!isMac) return groups.map((g) => ({ title: g.title, items: g.items.slice() }));
  return groups.map((g) => ({
    title: g.title,
    items: g.items.map((it) => ({ ...it, keys: it.keys.map((k) => MAC_CAPS[k] || k) })),
  }));
}

/** True on a Mac (the desktop shell's or a browser's), false in node. */
export function isMacLike(): boolean {
  const nav = (globalThis as { navigator?: { platform?: string; userAgent?: string } }).navigator;
  if (!nav) return false;
  return /Mac|iPhone|iPad/.test(nav.platform || "") || /Mac OS X/.test(nav.userAgent || "");
}

/** The words a cap also answers to in the filter: "ctrl" finds ⌘ on a Mac,
 * "minus" finds −, "question" finds ?. */
const CAP_WORDS: Record<string, string> = {
  "⌘": "cmd command ctrl",
  "⌥": "alt option",
  Ctrl: "control",
  "−": "- minus",
  "+": "plus =",
  "?": "question help",
  Esc: "escape",
  Arrows: "arrow left right up down",
  Wheel: "scroll mouse",
};

function haystack(group: string, it: ShortcutItem): string {
  const caps = it.keys.map((k) => k + " " + (CAP_WORDS[k] || "")).join(" ");
  return (group + " " + it.keys.join("+") + " " + caps + " " + it.what).toLowerCase();
}

/** The caps of an item and the words they answer to, one token each. */
function capTokens(it: ShortcutItem): Set<string> {
  return new Set(it.keys.flatMap((k) => [k, ...(CAP_WORDS[k] || "").split(" ")]).map((t) => t.toLowerCase()));
}

/** The sheet's filter: the items whose keys, words or group hold EVERY word
 * of `query` (case-folded; "ctrl z", "ctrl+z", "undo" all find Undo). A
 * one-character word is a KEY ("z", "1", "?", "+"): it matches a cap, never
 * a letter inside a word ("ctrl z" doesn't find Ctrl+Wheel's "zoom"). A
 * group with nothing left drops out; an empty query keeps everything. */
export function filterShortcuts(groups: readonly ShortcutGroup[], query: string): ShortcutGroup[] {
  const q = query.trim().toLowerCase();
  if (!q) return groups.slice();
  // "+" joins caps ("ctrl+z"), unless it is the whole query (Zoom in).
  const split = q.split(/[\s+]+/).filter(Boolean);
  const words = split.length ? split : [q];
  const out: ShortcutGroup[] = [];
  for (const g of groups) {
    const items = g.items.filter((it) => {
      const hay = haystack(g.title, it);
      const caps = capTokens(it);
      return words.every((w) => (w.length === 1 ? caps.has(w) : hay.includes(w)));
    });
    if (items.length) out.push({ title: g.title, items });
  }
  return out;
}
