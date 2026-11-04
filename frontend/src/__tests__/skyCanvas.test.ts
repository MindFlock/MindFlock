/** Round 17 (docs/flights.md "Round 17: the Sky as a canvas tool", task
 * `sky-core`): the manners every good canvas has — fit selection (Shift+2),
 * Shift+1 / Shift+0, the eased camera, redo, Tab to the
 * palette, the cursor contract, focus mode, and the seams mounted (Minimap,
 * OffscreenHints, ZoomMenu, SkyShortcuts). The pure parts run; the wiring
 * is pinned in SkyView's and Sky.css's text. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code, css, src } from "./sourceText";
import { CAM_EASE_MS, ZOOM_MAX, ZOOM_MIN, easeOut, fitCamera, fitSelection, lerpCamera, sameCamera } from "../lib/skyLayout";
import { clearUndo, popRedo, popUndo, pushUndo, redoCount, takeUndo, undoAction, undoCount } from "../lib/skyUndo";

const v = code("components/flights/SkyView.tsx");
const sheet = css("components/flights/Sky.css");

describe("fit selection (Shift+2)", () => {
  it("fits the selected boxes' bounds; nothing selected: null", () => {
    expect(fitSelection([], 1400, 900)).toBeNull();
    const one = fitSelection([{ x: 100, y: 100, w: 400, h: 200 }], 1400, 900);
    expect(one).toEqual(fitCamera({ x: 100, y: 100, w: 400, h: 200 }, 1400, 900, 1.25));
    const two = fitSelection(
      [
        { x: 100, y: 100, w: 400, h: 200 },
        { x: 900, y: 600, w: 300, h: 100 },
      ],
      1400,
      900
    )!;
    expect(two).toEqual(fitCamera({ x: 100, y: 100, w: 1100, h: 600 }, 1400, 900, 1.25));
    expect(two.z).toBeLessThanOrEqual(1.25);
    expect(two.z).toBeGreaterThanOrEqual(ZOOM_MIN);
  });

  it("the sky binds Shift+1 (fit all), Shift+2 (fit selection), Shift+0 (100 %) by key position, keeping 0 and 1", () => {
    expect(v).toContain('const digit = (n: string) => e.code === "Digit" + n || e.code === "Numpad" + n;');
    expect(v).toMatch(/e\.shiftKey && \(digit\("1"\) \|\| k === "!"\)\) \{\s*handled\(\);\s*fitAll\(\);/);
    expect(v).toMatch(/e\.shiftKey && \(digit\("2"\) \|\| k === "@"\)\) \{\s*handled\(\);\s*fitSel\(\);/);
    expect(v).toMatch(/e\.shiftKey && \(digit\("0"\) \|\| k === "\)"\)\) \{\s*handled\(\);\s*zoomTo\(1\);/);
    expect(v).toContain('} else if (k === "0") {');
    expect(v).toContain('} else if (k === "1") {');
    expect(v).toContain("const target = selBox ? fitSelection([selBox], vp.w, vp.h, selOne ? 1.5 : 1.25) : null;");
  });
});

describe("the eased camera", () => {
  it("ease-out starts fast and lands exactly at 1; the lerp ends exactly on the target", () => {
    expect(CAM_EASE_MS).toBe(220);
    expect(easeOut(0)).toBe(0);
    expect(easeOut(1)).toBe(1);
    expect(easeOut(0.5)).toBeGreaterThan(0.5);
    expect(easeOut(2)).toBe(1);
    const from = { x: 0, y: 0, z: 0.5 };
    const to = { x: -300, y: 120, z: 2 };
    expect(lerpCamera(from, to, 0)).toEqual(from);
    expect(lerpCamera(from, to, 1)).toEqual(to);
    expect(lerpCamera(from, to, 1.5)).toEqual(to);
    const mid = lerpCamera(from, to, 0.5);
    expect(mid.x).toBe(-150);
    expect(mid.y).toBe(60);
    // The zoom goes in log space: halfway between 0.5 and 2 is 1.
    expect(mid.z).toBeCloseTo(1, 6);
    expect(sameCamera(to, { ...to })).toBe(true);
    expect(sameCamera(to, { ...to, x: to.x + 1 })).toBe(false);
  });

  it("fit, focus and jump go through goTo (requestAnimationFrame, the last frame the target); the first picture, reduced motion and any direct move are instant", () => {
    expect(v).toMatch(/const goTo = useCallback\(\s*\(target: SkyCamera, instant = false\) => \{\s*stopEase\(\);/);
    expect(v).toContain("if (instant || motionOff || sameCamera(from, target) || typeof requestAnimationFrame !== \"function\" || typeof performance === \"undefined\") {");
    expect(v).toContain("const t = (now - t0) / CAM_EASE_MS;");
    expect(v).toMatch(/if \(t >= 1\) \{\s*ease\.current = 0;\s*setCamState\(target\);\s*return;\s*\}/);
    expect(v).toContain("setCamState(lerpCamera(from, target, easeOut(t)));");
    // Every other setCam stops a glide first.
    expect(v).toMatch(/const setCam: typeof setCamState = useCallback\(\s*\(next\) => \{\s*stopEase\(\);\s*setCamState\(next\);/);
    expect(v).toMatch(/const startPan = [^{]*\{\s*stopEase\(\);/);
    expect(v).toContain("goTo((whole ? fitCamera : firstFit)(boundsOf(rects), vp.w, vp.h, mode === \"one\" ? 1.5 : 1), instant);");
    expect(v).toContain("goTo(fitCamera(r, vp.w, vp.h, 1.5), instant);");
    expect(v).toContain("goTo(centerOn(r, vp.w, vp.h, Math.max(c.z, 0.85)));");
    expect(v).toMatch(/const zoomTo = \(z: number\) => \{\s*autoFit\.current = false;\s*goTo\(zoomAt\(camRef\.current, z, vp\.w \/ 2, vp\.h \/ 2\)\);/);
    // No FLIP ghosts: the glide is the plane's transform alone.
    expect(v).not.toMatch(/flyGhosts|FLIP_MS/);
  });
});

describe("redo (lib/skyUndo)", () => {
  beforeEach(() => {
    clearUndo();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-31T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("an undo moves an entry with redo() to the redo stack; popRedo puts it back on the undo stack", () => {
    const undo = vi.fn(async () => undefined);
    const redo = vi.fn(async () => undefined);
    pushUndo({ label: "Remove “a”", undo, redo });
    expect(redoCount()).toBe(0);
    const e = popUndo()!;
    expect(e.label).toBe("Remove “a”");
    expect(redoCount()).toBe(1);
    expect(undoCount()).toBe(0);
    const again = popRedo()!;
    expect(again.label).toBe("Remove “a”");
    expect(again.redo).toBe(redo);
    expect(redoCount()).toBe(0);
    expect(undoCount()).toBe(1);
    expect(popUndo()?.id).toBe(e.id);
    expect(redoCount()).toBe(1);
  });

  it("an entry without redo() leaves nothing to redo", () => {
    pushUndo({ label: "Close “a”", undo: async () => undefined });
    popUndo();
    expect(redoCount()).toBe(0);
    expect(popRedo()).toBeNull();
  });

  it("a new action forgets the redo branch", () => {
    pushUndo({ label: "a", undo: async () => undefined, redo: async () => undefined });
    popUndo();
    expect(redoCount()).toBe(1);
    pushUndo({ label: "b", undo: async () => undefined });
    expect(redoCount()).toBe(0);
  });

  it("the toast's Undo moves the entry to the redo stack too (one road, one stack)", () => {
    const e = pushUndo({ label: "a", undo: async () => undefined, redo: async () => undefined });
    expect(takeUndo(e)).toBe(true);
    expect(redoCount()).toBe(1);
    const f = pushUndo({ label: "b", undo: async () => undefined, redo: async () => undefined });
    undoAction(f).onClick();
    // The push emptied the redo stack; the toast's Undo put "b" on it.
    expect(redoCount()).toBe(1);
    expect(popRedo()?.label).toBe("b");
  });

  it("the redo stack expires like the undo stack (10 minutes)", () => {
    pushUndo({ label: "a", undo: async () => undefined, redo: async () => undefined });
    popUndo();
    vi.advanceTimersByTime(10 * 60_000);
    expect(redoCount()).toBe(0);
    expect(popRedo()).toBeNull();
    // clearUndo empties both.
    pushUndo({ label: "c", undo: async () => undefined, redo: async () => undefined });
    popUndo();
    clearUndo();
    expect(redoCount()).toBe(0);
  });

  it("the sky binds Ctrl+Shift+Z / ⌘⇧Z and says Redid: …; the grounded delete, the merge and the note delete carry redo()", () => {
    expect(v).toContain("if (redoKey(e)) return undoNow(e, true);");
    expect(v).toContain('const redoKey = (e: KeyboardEvent) => (e.ctrlKey || e.metaKey) && !e.altKey && e.shiftKey && (e.key === "z" || e.key === "Z");');
    expect(v).toMatch(/const again = popRedo\(\);\s*if \(!again\?\.redo\) \{\s*toast\("Nothing to redo"\);\s*return;\s*\}\s*toast\("Redid: " \+ again\.label\);\s*void again\.redo\(\);/);
    expect(v).toContain("redo: () => deleteFlight(cur),");
    expect(v).toContain("redo: async () => (cur ? deleteFlight(cur) : false),");
    expect(v).toContain("redo: async () => putNotes(notesRef.current.filter((x) => x.id !== id)),");
    // The note's toast shares the entry with Ctrl+Z now.
    expect(v).toContain('toast("Note removed", { action: undoAction(entry, "Undo: put the note back") });');
  });
});

describe("Tab → the palette; double-click → a new session", () => {
  it("Tab floats the palette with its filter focused, a second Tab (in the filter) docks it; a double-click on empty sky opens the new-session block (what the guide and shelf say)", () => {
    expect(v).toMatch(/k === "Tab" && !e\.shiftKey && !narrow\) \{\s*handled\(\);\s*if \(paletteOn\) setPaletteMode\("shelf"\);\s*else floatPaletteAt\(paletteSpot\(\)\);/);
    expect(v).toContain('if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && paletteOn && typing(el) && el?.closest(".sky-palette-slot")) {');
    expect(v).toMatch(/const floatPaletteAt = \(at: Pos\) => \{\s*setFilterFocus\(true\);\s*onLayout\(PALETTE_KEY, \{ x: Math\.round\(at\.x\), y: Math\.round\(at\.y\), pinned: true \}\);\s*setPaletteMode\("float"\);/);
    expect(v).toContain("focusFilter={!!catalogOpen || filterFocus}");
    expect(v).toMatch(/const onDouble = \(e: ReactMouseEvent<HTMLDivElement>\) => \{\s*if \(!ground\(e\.target\)\) return;\s*newAtScreen\(e\.clientX, e\.clientY\);\s*\};/);
    expect(v).toContain("onDoubleClick={onDouble}");
    // Docking forgets the filter focus, so the next float does not steal the keyboard.
    expect(v).toMatch(/if \(m === "shelf"\) \{\s*setCatalogOpen\(null\);\s*setFilterFocus\(false\);/);
  });
});

describe("the cursor contract (Sky.css)", () => {
  it("grab on a bar, grabbing while dragging, crosshair for the marquee, move while snapping, copy over a taking line, never zoom-in / zoom-out", () => {
    expect(sheet).toMatch(/\.sky-viewport :is\(\.skl-bar, \.sky-line-bar, \.sky-lane-head, \[data-sky-head\][^)]*\) \{\s*cursor: grab;/);
    expect(sheet).toMatch(/\.is-dragging[^{]*\)\s*:is\(\.skl-bar, \.sky-line-bar[^{]*\{\s*cursor: grabbing;/);
    expect(sheet).toMatch(/\.sky\[data-tool="drag"\] \.sky-viewport \{\s*cursor: crosshair;/);
    expect(sheet).toMatch(/\.sky-line-slot\.is-snapping :is\(\.skl-bar, \.sky-line-bar\)[^{]*\{\s*cursor: move;/);
    expect(sheet).toMatch(/\.sky-viewport :is\(\.is-drop-target, \.is-merge-target\)[^{]*\{\s*cursor: copy;/);
    expect(sheet).not.toMatch(/zoom-in|zoom-out/);
    // Click mode's hand stays (round 12).
    expect(sheet).toMatch(/\.sky\[data-tool="click"\] \.sky-viewport,[^{]*\.fl-box[^{]*\{\s*cursor: grab;/);
    expect(v).toContain('(drag?.from[l.id] && guides.length ? " is-snapping" : "") +');
  });
});

describe("focus mode (F)", () => {
  it("F with a selection dims everything not selected to 35 %; any press or Esc lifts it, so does clearing the selection", () => {
    expect(v).toMatch(/\(k === "f" \|\| k === "F"\) && !selectionEmpty\(sel\)\) \{\s*handled\(\);\s*setFocusMode\(true\);/);
    expect(v).toMatch(/if \(e\.key === "Escape" && focusMode && !useUi\.getState\(\)\.openDialog\) \{\s*e\.preventDefault\(\);\s*setFocusMode\(false\);\s*return;/);
    expect(v).toMatch(/const onDownCapture = [^{]*\{\s*if \(focusMode\) setFocusMode\(false\);/);
    expect(v).toMatch(/const clearSel = useCallback\(\(\) => \{\s*setSel\(NO_SELECTION\);\s*setSelAsk\(""\);\s*setFocusMode\(false\);/);
    expect(v).toContain("const dimmed = (id: string) => focusMode && !sel.lines.has(id) && selOne?.line !== id;");
    expect(v).toContain('className={"sky-plane" + (focusMode ? " sky-focus" : "")}');
    expect(v).toContain('data-dim={dimmed(l.id) ? "" : undefined}');
    expect(v).toContain('data-dim={dimmed(o.key) ? "" : undefined}');
    expect(v).toContain('data-dim={dimmed(INTAKE_KEY) ? "" : undefined}');
    expect(v).toContain('data-dim={dimmed(PALETTE_KEY) ? "" : undefined}');
    expect(sheet).toMatch(/\.sky-plane\.sky-focus \[data-dim\] \{\s*opacity: 0\.35;\s*transition: opacity 0\.12s ease;/);
  });
});

describe("the seams are mounted", () => {
  it("Minimap + OffscreenHints bottom-right with every line's and object's rect, tone, label, selected and focused; open remembered in mf_sky_minimap", () => {
    expect(v).toContain('import { Minimap, OffscreenHints, type MinimapRect } from "./Minimap";');
    expect(v).toContain('const MINIMAP_KEY = "mf_sky_minimap";');
    expect(v).toContain('const [minimapOpen, setMinimapOpen] = useState(() => readPref(MINIMAP_KEY) !== "0");');
    expect(v).toContain("<OffscreenHints rects={mapRects} cam={cam} viewport={vp} onJump={jumpTo} />");
    // Round 21: folded while it would cover a route (mmShown), a move on it is the owner's (userCam).
    expect(v).toMatch(/<div className="sky-minimap-host" ref=\{mmHost\}[^>]*>\s*<Minimap\s+rects=\{mapRects\}\s+cam=\{cam\}\s+viewport=\{vp\}\s+onCamera=\{\(c\) => userCam\(c\)\}\s+onJump=\{jumpTo\}\s+open=\{mmShown\}/);
    expect(v).toContain('kind: l.flight ? "grounded" : "line",');
    expect(v).toContain('tone: l.flight ? l.flight.state : row ? deriveSessionState(row).state : "",');
    expect(v).toContain("focused: l.flight ? groundFocus === l.id : focusLine === l.id,");
    expect(v).toContain('kind: o.def.kind as MinimapRect["kind"]');
    // The jump: eased camera, the line focused as a press would (no box opens).
    expect(v).toMatch(/const jumpTo = \(id: string\) => \{\s*const r = rectOf\(id\);\s*if \(!r\) return;\s*goTo\(centerOn\(r, vp\.w, vp\.h, Math\.max\(camRef\.current\.z, 0\.85\)\)\);/);
    expect(sheet).toMatch(/\.sky-minimap-host \{\s*position: absolute;\s*right: 12px;\s*bottom: 12px;/);
  });

  it("ZoomMenu replaces the readout with min / max / the fits; SkyShortcuts opens on ? and closes on Esc, with the real groups", () => {
    expect(v).not.toContain('<span className="sky-zoom-read"');
    expect(v).toMatch(/<ZoomMenu\s+zoom=\{cam\.z\}\s+min=\{ZOOM_MIN\}\s+max=\{ZOOM_MAX\}\s+onZoomTo=\{zoomTo\}\s+onFitAll=\{\(\) => fitAll\(\)\}\s+onFitSelection=\{fitSel\}\s+onFitFocused=\{\(\) => fitFocused\(\)\}\s+canFitSelection=\{!selectionEmpty\(sel\)\}\s+canFitFocused=\{!!\(focusLine \|\| oneLine\)\}/);
    expect(ZOOM_MIN).toBe(0.1);
    expect(ZOOM_MAX).toBe(3);
    expect(v).toMatch(/k === "\?"\) \{\s*handled\(\);\s*setShortcutsOpen\(true\);/);
    expect(v).toMatch(/if \(e\.key === "Escape" && shortcutsOpen\) \{\s*e\.preventDefault\(\);\s*setShortcutsOpen\(false\);\s*return;/);
    // The groups come from lib/skyShortcuts — wired (the sheet was empty: round-consolidate defect 1).
    expect(v).toContain("<SkyShortcuts open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} groups={SKY_SHORTCUT_GROUPS} />");
    expect(src("components/flights/SkyView.tsx")).not.toContain("TODO(zoom-shortcuts)");
    // The ⋯ menu names the sheet too, for the mouse.
    expect(v).toContain('{ key: "keys", label: "Keyboard shortcuts…", title: "Every key the sky knows (?)", onSelect: () => setShortcutsOpen(true) },');
  });
});
