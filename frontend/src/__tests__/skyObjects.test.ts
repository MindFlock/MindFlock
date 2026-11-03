/** The sky objects registry (lib/skyObjects; docs/flights.md "As built:
 * round 15 objects"): one list for the intake lane, the palette, the landing
 * strip, the device and people blocks and the notes — their keys, where they
 * are placed, their frames, which are on the board; the right column
 * (lib/skyLayout `rightColumn`); the notes' positions and drops; and the
 * people / device blocks and the sticky note, server-rendered. The SkyView
 * wiring (one loop, the drag onto a block, Delete on a note, the ⋯ and the
 * right-click menu) is pinned in the source. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import type { Device, Instance } from "../api/types";
import type { SkyNote } from "../api/skyApi";
import type { PeerLink } from "../lib/peer";
import type { SkyObjectData } from "../lib/skyObjects";

vi.mock("../api/client", async () => ({
  ...(await vi.importActual<typeof import("../api/client")>("../api/client")),
  api: vi.fn(() => new Promise(() => {})),
  instApi: vi.fn(() => new Promise(() => {})),
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const sky = await import("../lib/skyLayout");
const O = await import("../lib/skyObjects");
const { PersonBlock, shareBlockReason } = await import("../components/flights/PeopleBlocks");
const { DeviceBlock, deviceBlockReason, deviceMoveError, NO_DEVICE_MOVE } = await import("../components/flights/DeviceBlocks");
const { StickyNote, NOTE_MAX_CHARS } = await import("../components/flights/StickyNote");
const { ApiError } = await import("../api/client");

const link = (over: Partial<PeerLink> = {}): PeerLink =>
  ({ link_id: "L1", peer_name: "Bob", role: "listener", sas: "12 34", perms: { messages: true, diff: true, read_file: true }, shared: false, connected: true, sas_verified: true, ...over }) as PeerLink;
const device = (over: Partial<Device> = {}): Device => ({ device: "mini", host: "mac-mini", os: "darwin", connected: true, sessions: 2, ...over });
const note = (over: Partial<SkyNote> = {}): SkyNote => ({ id: "n1", text: "ship it", x: 100, y: 50, w: 200, h: 120, line: "", updated: 1, ...over });
const row = (title: string, over: Partial<Instance> = {}): Instance => ({ title, branch: "", ...over }) as Instance;

const data = (over: Partial<SkyObjectData> = {}): SkyObjectData => ({ mode: "all", narrow: false, intakeOn: true, paletteOn: false, links: [], devices: [], notes: [], ...over });

describe("the registry", () => {
  it("one entry per kind, in placement order: left (lane, palette), right (strip, devices, people), free (notes)", () => {
    expect(O.SKY_OBJECTS.map((d) => d.kind)).toEqual(["intake", "palette", "landing", "device", "person", "note"]);
    expect(O.SKY_OBJECTS.map((d) => d.place)).toEqual(["left", "left", "right", "right", "right", "free"]);
    expect(O.SKY_OBJECTS.map((d) => d.key)).toEqual(["intake", "palette", "landing", "device:", "person:", "note:"]);
    expect(O.SKY_OBJECTS.filter((d) => d.accepts === "session").map((d) => d.kind)).toEqual(["device", "person"]);
    // Every def has what the host needs.
    for (const d of O.SKY_OBJECTS) {
      expect(typeof d.frame).toBe("function");
      expect(typeof d.size).toBe("function");
      expect(typeof d.render).toBe("function");
      expect(d.min.w).toBeLessThanOrEqual(d.max.w);
      expect(d.className.length).toBeGreaterThan(0);
    }
  });

  it("the keys", () => {
    expect([O.personKey("L1"), O.deviceKey("mini"), O.noteKey("n1")]).toEqual(["person:L1", "device:mini", "note:n1"]);
    expect(["intake", "palette", "landing", "person:L1", "device:mini", "note:n1", "fix-login", "flight:f1"].map(O.objectKind)).toEqual(["intake", "palette", "landing", "person", "device", "note", "", ""]);
    expect(O.isObjectKey("landing")).toBe(true);
    expect(O.isObjectKey("fix-login")).toBe(false);
    expect(O.noteIdOf("note:n1")).toBe("n1");
    expect(O.noteIdOf("landing")).toBe("");
    expect(O.newNoteId()).toMatch(/^n_/);
    expect(O.newNoteId()).not.toBe(O.newNoteId());
  });

  it("which objects are on the board: the lane when on, the palette when floating, the strip and blocks in All / Mine / Perched, never on a phone or in This session, a note per note", () => {
    const d = data({ links: [link()], devices: [device(), device({ device: "pi", host: "pi" })], notes: [note(), note({ id: "n2" })] });
    expect(O.skyObjects(d).map((o) => o.key)).toEqual(["intake", "landing", "device:mini", "device:pi", "person:L1", "note:n1", "note:n2"]);
    expect(O.skyObjects({ ...d, paletteOn: true }).map((o) => o.key).slice(0, 2)).toEqual(["intake", "palette"]);
    expect(O.skyObjects({ ...d, intakeOn: false, mode: "one" }).map((o) => o.key)).toEqual([]);
    expect(O.skyObjects({ ...d, narrow: true }).map((o) => o.key)).toEqual(["intake"]);
    expect(O.skyObjects({ ...d, mode: "perched" }).map((o) => o.key)).toContain("landing");
    // The item carries its data.
    const items = O.skyObjects(d);
    expect((items.find((o) => o.key === "person:L1")!.data as PeerLink).peer_name).toBe("Bob");
    expect((items.find((o) => o.key === "note:n2")!.data as SkyNote).id).toBe("n2");
  });

  it("frames and sizes: the lane's and the palette's as before, the strip's range, a block's fixed width, a note's own numbers", () => {
    const [intake, palette, landing, dev, , noteDef] = O.SKY_OBJECTS;
    expect(intake.size(undefined, undefined)).toEqual({ w: sky.INTAKE_W, h: 320 });
    expect(intake.size({ open: true }, undefined)).toEqual({ w: sky.INTAKE_OPEN_W, h: 520 });
    expect(intake.size(undefined, { w: 100, h: 900 })).toEqual({ w: sky.INTAKE_W, h: 900 });
    expect(intake.frame({ w: 10, h: 10 })).toEqual({ w: sky.INTAKE_MIN_W, h: sky.INTAKE_MIN_H });
    expect(palette.size(undefined, undefined)).toEqual({ w: sky.PALETTE_W, h: sky.PALETTE_H });
    expect(palette.frame({ w: 500, h: 300 })).toEqual({ w: 500, h: 300 });
    expect(landing.size(undefined, undefined)).toEqual({ w: sky.LANDING_W, h: sky.LANDING_H });
    expect(landing.size(undefined, { w: 320, h: 410 })).toEqual({ w: 320, h: 410 });
    expect(landing.frame({ w: 5000, h: 10 })).toEqual({ w: sky.LANDING_MAX.w, h: sky.LANDING_MIN.h });
    expect(sky.landingFrame(undefined)).toBeNull();
    expect(dev.size(undefined, undefined)).toEqual({ w: sky.BLOCK_W, h: sky.BLOCK_H });
    expect(dev.frame({ w: 1, h: 1 })).toBeNull();
    expect(noteDef.size({ w: 300, h: 90 }, undefined)).toEqual({ w: 300, h: 90 });
    expect(O.noteSize({ w: 10, h: 9999 })).toEqual({ w: sky.NOTE_MIN.w, h: sky.NOTE_MAX.h });
    expect(O.noteSize({ w: NaN, h: 100 })).toEqual({ w: sky.NOTE_W, h: sky.NOTE_H });
  });

  it("the wrapper's class carries the drag, selection and drop-target states", () => {
    const [, , landing] = O.SKY_OBJECTS;
    expect(O.objectClass(landing, {})).toBe("sky-object sky-object-landing");
    expect(O.objectClass(landing, { dragging: true, selected: true, target: true })).toBe("sky-object sky-object-landing is-dragging is-selected is-merge-target");
  });
});

describe("placement", () => {
  it("the right column sits just right of everything placed, stacked from the top; empty, where the first line would", () => {
    const placed = [
      { x: 40, y: 40, w: 240, h: 300 },
      { x: 400, y: 40, w: 500, h: 200 },
      { x: 400, y: 300, w: 300, h: 100 },
    ];
    const col = sky.rightColumn(placed, [
      { id: "landing", w: 300, h: 240 },
      { id: "device:mini", w: 200, h: 64 },
      { id: "person:L1", w: 200, h: 64 },
    ]);
    expect(col.landing).toEqual({ x: 900 + sky.SKY_GAP, y: sky.SKY_MARGIN });
    expect(col["device:mini"]).toEqual({ x: 900 + sky.SKY_GAP, y: sky.SKY_MARGIN + 240 + sky.SKY_GAP / 2 });
    expect(col["person:L1"].y).toBe(col["device:mini"].y + 64 + sky.SKY_GAP / 2);
    expect(sky.rightColumn([], [{ id: "landing", w: 300, h: 240 }])).toEqual({ landing: { x: sky.LINES_LEFT, y: sky.SKY_MARGIN } });
    expect(sky.rightColumn(placed, [])).toEqual({});
  });

  it("a note sits at its own numbers, or relative to its line; a drop attaches or frees it", () => {
    const at = (id: string) => (id === "fix-login" ? { x: 500, y: 300 } : null);
    expect(O.notePos(note(), at)).toEqual({ x: 100, y: 50 });
    expect(O.notePos(note({ line: "fix-login", x: 20, y: -30 }), at)).toEqual({ x: 520, y: 270 });
    // The line is gone: the note's own numbers stand.
    expect(O.notePos(note({ line: "gone", x: 20, y: -30 }), at)).toEqual({ x: 20, y: -30 });
    const dropped = O.noteDropped(note(), { x: 530, y: 280 }, "fix-login", { x: 500, y: 300 });
    expect(dropped).toMatchObject({ line: "fix-login", x: 30, y: -20 });
    expect(O.noteDropped(dropped, { x: 10, y: 10 }, "", null)).toMatchObject({ line: "", x: 10, y: 10 });
  });

  it("hitAt: the topmost rect under the point", () => {
    const items = [
      { id: "a", rect: { x: 0, y: 0, w: 100, h: 100 } },
      { id: "b", rect: { x: 50, y: 50, w: 100, h: 100 } },
    ];
    expect(O.hitAt({ x: 10, y: 10 }, items)).toBe("a");
    expect(O.hitAt({ x: 75, y: 75 }, items)).toBe("b");
    expect(O.hitAt({ x: 500, y: 500 }, items)).toBe("");
  });
});

describe("SkyView iterates the one list", () => {
  const v = code("components/flights/SkyView.tsx");

  it("builds the objects from the registry and draws them in one loop; the lane and the palette keep their JSX", () => {
    expect(v).toContain("const objects = useMemo(() => skyObjects(objData), [objData]);");
    expect(v).toContain("{objects.map(objectFor)}");
    expect(v).toContain('if (o.def.kind === "intake" || o.def.kind === "palette") return <Fragment key={o.key}>{o.def.render(host)}</Fragment>;');
    expect(v).toContain("renderIntake,\n    renderPalette,");
    expect(v).not.toContain("{intakeOn && (");
    expect(v).not.toContain("{paletteOn && (");
    // The right column is placed after the lines, and its spots are kept like a line's.
    expect(v).toContain('const rightItems = objects.filter((o) => o.def.place === "right").map((o) => ({ id: o.key, ...objectBox(o) }));');
    expect(v).toContain("rightColumn(placedRects, rightItems.filter((r) => !saved[r.id]))");
    expect(v).toContain('for (const o of objects) if (o.def.kind === "note") place[o.key] = notePos(o.data as SkyNote, (id) => place[id] || null);');
    expect(v).toContain("objectById.has(id) ? true : id === INTAKE_KEY ? intakeOn");
  });

  it("every object gets the host: zoom, lod, its entry, its spot, the drag / resize / measure / layout handlers", () => {
    expect(v).toMatch(/const hostFor = \(o: SkyObjectItem\): SkyObjectHost => \(\{\s*key: o\.key,\s*zoom: cam\.z,\s*lod: lodFor\(cam\.z\),\s*layoutEntry: layout\[o\.key\],\s*pos: posOf\(o\.key\),\s*draggable: liveTool === "drag",\s*selected: sel\.lines\.has\(o\.key\),\s*dragging: !!drag\?\.from\[o\.key\],\s*target: mergeOver === o\.key,\s*onHeadDown: \(e\) => onDragStart\(o\.key, e\),\s*onResize: \(w, h\) => onLayout\(o\.key, \{ w, h \}\),\s*onMeasure: \(w, h\) => onMeasure\(o\.key, w, h\),\s*onLayout: \(patch\) => onLayout\(o\.key, patch\),/);
    // An object's head press focuses no session.
    expect(v).toMatch(/if \(id === INTAKE_KEY \|\| id === PALETTE_KEY\) return;\s*if \(isObjectKey\(id\)\) return;/);
    expect(v).toContain('if (el.closest(".skl-bar, .sky-lane-head, .sky-lane-rail, [data-sky-head]")) return "line";');
    expect(v).toContain('const obj = el.closest(".sky-object[data-object]");');
  });

  it("a session line dragged alone onto a person or device block asks the block; a note onto a line attaches", () => {
    expect(v).toContain("const toBlock = !kind && !merging && !isGroundedKey(id) && ids.length === 1;");
    expect(v).toContain('const toLine = kind === "note" && ids.length === 1;');
    expect(v).toContain('const blocks = toBlock ? objects.filter((o) => o.def.accepts === "session").map((o) => ({ id: o.key, rect: rectOf(o.key)! })) : [];');
    expect(v).toMatch(/if \(landed && toBlock\) \{[\s\S]*?setDropped\(\(d\) => \(\{ \.\.\.d, \[landed\]: id \}\)\);\s*return;/);
    expect(v).toMatch(/if \(toLine && g\.moved\) \{[\s\S]*?dropNote\(noteIdOf\(id\), \{ x: from\[id\]\.x \+ dx, y: from\[id\]\.y \+ dy \}, landed\);/);
    // A grounded line's merge keeps its own path (skyMerge.test pins it).
    expect(v).toContain('const into = merging ? landed : "";');
    expect(v).toContain('(mergeOver && !l.flight && drag?.from[l.id] ? " is-merging" : "")');
    // The merge hit test's own path for grounded lines is untouched.
    expect(v).toContain("merging ? mergeTargetAt(planeAt(ev.clientX, ev.clientY), others, id) : toBlock ? hitAt(");
    // A block's answer clears what it was asked.
    expect(v).toMatch(/answerDrop: \(key\) =>\s*setDropped\(\(d\) => \{\s*const \{ \[key\]: _gone, \.\.\.rest \} = d;\s*return rest;/);
  });

  it("notes: server-side, saved a moment after a change; Add a note from the ⋯ and the right-click menu; Delete with Undo", () => {
    expect(v).toContain("const notesQ = useSkyNotesQuery();");
    expect(code("lib/skyObjects.ts")).toContain('return useQuery({ queryKey: ["sky-notes"], queryFn: fetchSkyNotes, retry: false, staleTime: Infinity });');
    expect(v).toContain("noteSave.current = setTimeout(() => void saveSkyNotes(next).catch((e) => toast(\"Saving the notes failed: \" + errMsg(e))), 400);");
    expect(v).toContain('{ key: "note", label: "Add a note", title: "A sticky note in the middle of the view", onSelect: () => addNote(middle()) },');
    expect(v).toContain("onContextMenu={onGroundMenu}");
    expect(v).toMatch(/const onGroundMenu = [\s\S]*?if \(!ground\(e\.target\)\) return;\s*e\.preventDefault\(\);/);
    // Right-click on empty sky: add a block by search (QuickAdd), the note at its foot.
    expect(v).toMatch(/<QuickAdd[\s\S]*?onNote=\{\(\) => \{\s*addNote\(groundMenu\.at\);/);
    expect(v).toMatch(/const noteEl = el\?\.closest\("\.sky-note\[data-note\]"\);\s*if \(noteEl && !typing\(el\)\) \{[\s\S]*?removeNote\(noteEl\.getAttribute\("data-note"\) \|\| ""\);/);
    // Round 17: the note's removal is one lib/skyUndo entry (Ctrl+Z, the toast, Ctrl+Shift+Z).
    expect(v).toContain('toast("Note removed", { action: undoAction(entry, "Undo: put the note back") });');
    const api = code("api/skyApi.ts");
    expect(api).toContain('const d = await api<{ notes?: SkyNote[] }>("/api/sky/notes", { method: "PUT", json: { notes } });');
    expect(api).toContain('return instApi(title, "/move", { method: "POST", json: { device, force } });');
  });

  it("the objects glide only under Tidy, like the slots; a block lights as a drop target", () => {
    const c = cssOf("components/flights/SkyObjects.css");
    expect(c).toMatch(/\.sky-plane\[data-tidying\] \.sky-object \{\s*transition: transform 0\.28s ease;/);
    expect(c).toMatch(/\n\.sky-object \{[^}]*position: absolute;/);
    expect(c).toMatch(/\.sky-block\.is-merge-target \{[^}]*border-color: var\(--accent\);/);
  });
});

describe("a person block", () => {
  const html = (over: Partial<PeerLink> = {}, dropped = "", rows: Instance[] = [row("fix-login")]) =>
    renderToStaticMarkup(createElement(PersonBlock, { link: link(over), rows, dropped, onHeadDown() {}, onDone() {} }));

  it("names the peer, the state dot, and invites a drop", () => {
    const h = html();
    expect(h).toMatch(/^<div class="sky-block sky-person" data-person="L1" data-state="verified" role="group" aria-label="Bob — drop a chain here to share its session">/);
    expect(h).toContain('<div class="sky-block-head" data-sky-head="true" title="Drag to move">');
    expect(h).toContain('<strong class="sky-block-name">Bob</strong>');
    expect(h).toContain("Drop a chain to share it");
    expect(html({ connected: false })).toContain('data-state="offline"');
    expect(html({ sas_verified: false })).toContain('data-state="unverified"');
  });

  it("a dropped line asks inline: the mode, the I-understand tick for a full share, Share / Cancel — and the SAS gate", () => {
    const h = html({}, "fix-login");
    expect(h).toContain('<form class="sky-block-ask fl-form" data-no-drag="true">');
    expect(h).toContain("Share <strong>fix-login</strong> with Bob?");
    expect(h).toMatch(/<input type="radio" name="mode-L1" checked=""\/> Gated to its folder \(sandboxed copy\)/);
    expect(h).toContain("Full join (they type into it as it is)");
    expect(h).toContain('<button type="submit" class="fl-btn btn-primary">Share</button>');
    expect(h).toContain(">Cancel</button>");
    expect(h).not.toContain("I understand");
    // Unverified: no Share at all, the sentence instead.
    const gated = html({ sas_verified: false }, "fix-login");
    expect(gated).toContain("Press It matches in People first");
    expect(gated).not.toContain('type="submit"');
    expect(shareBlockReason(link({ sas_verified: false }), "fix-login", [row("fix-login")])).toContain("It matches");
    expect(shareBlockReason(link(), "fix-login", [row("fix-login", { parent: "lead" })])).toContain("Only a session of this machine");
    expect(shareBlockReason(link(), "fix-login", [row("fix-login")])).toBe("");
    const src = code("components/flights/PeopleBlocks.tsx");
    expect(src).toContain("await sharePeerSession(link.link_id, dropped, mode);");
    expect(src).toContain("const ok = canShare({ title: dropped, mode, understood, busy });");
    expect(src).toContain('{mode === "full" && (');
    expect(src).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
    // Shared: the word on the block, from what the link already shares.
    expect(src).toContain(".then((v) => on && setShared(v.mine.map((m) => m.title)))");
    expect(src).toContain("Shared · {shared.length === 1 ? windowName(shared[0]) : shared.length + \" sessions\"}");
  });
});

describe("a device block", () => {
  const html = (over: Partial<Device> = {}, dropped = "") => renderToStaticMarkup(createElement(DeviceBlock, { device: device(over), dropped, onHeadDown() {}, onDone() {} }));

  it("names the device, its state, its sessions; a dropped line asks to move with the mid-turn tick", () => {
    const h = html();
    expect(h).toMatch(/^<div class="sky-block sky-device" data-device="mini" data-state="on" role="group" aria-label="mac-mini — drop a chain here to move its session there">/);
    expect(h).toContain("darwin · 2 sessions");
    const ask = html({}, "fix-login");
    expect(ask).toContain("Move <strong>fix-login</strong> to mac-mini? Its agent pauses while it moves.");
    expect(ask).toContain("Even if it is mid-turn");
    expect(ask).toContain('<button type="submit" class="fl-btn btn-primary">Move</button>');
    const off = html({ connected: false }, "fix-login");
    expect(off).toContain("Not connected");
    expect(off).not.toContain('type="submit"');
    expect(deviceBlockReason({ connected: true })).toBe("");
  });

  it("calls the existing move route with the device; a folder-only server's refusal is named plainly", () => {
    const src = code("components/flights/DeviceBlocks.tsx");
    expect(src).toContain("await moveSessionToDevice(dropped, device.device, force);");
    expect(src).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
    expect(deviceMoveError(new ApiError(400, "bad_dest", { error: "bad_dest" }))).toBe(NO_DEVICE_MOVE);
    expect(deviceMoveError(new ApiError(409, "working", { error: "working" }))).toContain("mid-turn");
    expect(deviceMoveError(new Error("boom"))).toBe("boom");
  });
});

describe("a sticky note", () => {
  const html = (over: Partial<SkyNote> = {}, props: Record<string, unknown> = {}) =>
    renderToStaticMarkup(createElement(StickyNote, { note: note(over), onHeadDown() {}, onRemove() {}, onResize() {}, onEdit() {}, onChange() {}, ...props }));

  it("its head, its text, its corner, its ×; a pin when attached; editing is a textarea", () => {
    const h = html();
    expect(h).toMatch(/^<div class="sky-note" data-note="n1" data-sky-box="true" tabindex="-1" role="note" aria-label="Note: ship it" style="width:200px;height:120px">/);
    expect(h).toContain('<div class="sky-note-head" data-sky-head="true" title="Drag to move the note">');
    expect(h).toContain('<div class="sky-note-text" title="Click to edit">ship it</div>');
    expect(h).toContain('<button type="button" class="fl-icon-btn sky-note-x" aria-label="Remove the note" title="Remove (Delete)" data-no-drag="true">×</button>');
    expect(h).toContain('<span class="sky-note-resize skl-resize" role="separator" tabindex="0" data-no-drag="true" aria-label="Resize the note"');
    expect(html({ text: "" })).toContain('<div class="sky-note-text is-empty" title="Click to edit">Write a note…</div>');
    expect(html({ line: "fix-login" }, { attached: true })).toContain('class="sky-note is-attached"');
    const editing = html({}, { editing: true });
    expect(editing).toContain('class="sky-note is-editing"');
    expect(editing).toMatch(/<textarea class="sky-note-area" maxLength="2000" placeholder="Write a note…">ship it<\/textarea>/);
    expect(NOTE_MAX_CHARS).toBe(2000);
  });

  it("blur saves, Esc cancels, Delete on the focused note removes, Enter edits; the corner's delta is over the zoom", () => {
    const src = code("components/flights/StickyNote.tsx");
    expect(src).toContain("onBlur={save}");
    expect(src).toMatch(/const save = \(\) => \{\s*const text = draft\.slice\(0, NOTE_MAX_CHARS\);\s*if \(text !== note\.text\) onChange\?\.\(text\);\s*onEdit\?\.\(false\);/);
    expect(src).toMatch(/if \(e\.key === "Escape"\) \{[\s\S]*?setDraft\(note\.text\);\s*onEdit\?\.\(false\);/);
    expect(src).toMatch(/if \(\(e\.key === "Delete" \|\| e\.key === "Backspace"\) && onRemove\) \{[\s\S]*?onRemove\(\);/);
    expect(src).toContain('} else if (e.key === "Enter" && onEdit) {');
    expect(src).toContain("setLive({ w: Math.max(NOTE_MIN.w, Math.round(g.w + (e.clientX - g.sx) / z)), h: Math.max(NOTE_MIN.h, Math.round(g.h + (e.clientY - g.sy) / z)) });");
    expect(src).toContain("rootRef.current?.focus({ preventScroll: true });");
    expect(src).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
  });
});
