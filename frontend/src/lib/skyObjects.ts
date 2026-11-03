/** The sky objects registry (docs/flights.md "Round 15" → `objects`, "As
 * built: round 15 objects"): everything on the plane that is not a line —
 * the intake lane, the floating palette, the Behaviors board, the landing
 * strip, the people and device blocks, the sticky notes — described ONCE, so SkyView iterates one
 * list to place, draw, drag, size and measure them.
 *
 * `SKY_OBJECTS` holds one entry per KIND: its layout key (or key prefix for
 * the kinds with many — `person:<id>`, `device:<name>`, `note:<id>`), where
 * it is placed (`left`: before the lines, in `assignSpots`'s order; `right`:
 * a column right of every line, `rightColumn`; `free`: wherever it was put,
 * pinned), its frame's range, what it accepts dropped on it, and how it is
 * drawn. `skyObjects(view)` expands the kinds into the list SkyView draws —
 * one `SkyObjectDef` per object on the board, in placement order.
 *
 * `render(host)` gets everything an object needs from the sky: the camera's
 * zoom and band, its layout entry and spot, whether it may be dragged, is
 * selected, is being dragged or is a drop target, and the callbacks that
 * reach the layout store — plus `host.view`, the view's own data and
 * handlers (rows, plans, the landing data, the links, the devices, the
 * notes, the drop confirms). The intake lane and the palette keep their JSX
 * in SkyView (`view.renderIntake` / `view.renderPalette`): their props are
 * the view's closures, pinned by the round 12/13 tests. */

import { useQuery } from "@tanstack/react-query";
import { createElement, type DragEvent as ReactDragEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { Device, Instance, TestPlan } from "../api/types";
import { fetchLanding, fetchSkyNotes, type LandingData, type SkyNote } from "../api/skyApi";
import { BEHAVIORS_H, BEHAVIORS_KEY, BEHAVIORS_MAX, BEHAVIORS_MIN, BEHAVIORS_W, BehaviorsBoard } from "../components/flights/BehaviorsBoard";
import { DeviceBlock } from "../components/flights/DeviceBlocks";
import { LandingStrip } from "../components/flights/LandingStrip";
import { PersonBlock } from "../components/flights/PeopleBlocks";
import { StickyNote } from "../components/flights/StickyNote";
import { PALETTE_KEY } from "../components/flights/SkyPalette";
import type { FlightPlan } from "./flights";
import type { PeerLink } from "./peer";
import type { CatalogDrop } from "./stopCatalog";
import {
  BLOCK_H,
  BLOCK_W,
  INTAKE_KEY,
  INTAKE_MAX_H,
  INTAKE_MAX_W,
  INTAKE_MIN_H,
  INTAKE_MIN_W,
  INTAKE_W,
  LANDING_H,
  LANDING_KEY,
  LANDING_MAX,
  LANDING_MIN,
  LANDING_W,
  NOTE_H,
  NOTE_MAX,
  NOTE_MIN,
  NOTE_W,
  PALETTE_H,
  PALETTE_MAX,
  PALETTE_MIN,
  PALETTE_W,
  intakeSize,
  objectFrame,
  type Lod,
  type Pos,
  type Rect,
  type SkyLineLayout,
  type SkyMode,
  type StoredLine,
} from "./skyLayout";

export type SkyPlace = "left" | "right" | "free";
export type SkyObjectKindId = "intake" | "palette" | "behaviors" | "landing" | "person" | "device" | "note";
export interface Size {
  w: number;
  h: number;
}

/** The keys of the kinds with many objects. */
export const personKey = (linkId: string) => "person:" + linkId;
export const deviceKey = (name: string) => "device:" + name;
export const noteKey = (id: string) => "note:" + id;
export const isNoteKey = (key: string) => key.startsWith("note:");
export const noteIdOf = (key: string) => (isNoteKey(key) ? key.slice(5) : "");
export const isPersonKey = (key: string) => key.startsWith("person:");
export const isDeviceKey = (key: string) => key.startsWith("device:");
/** The kind of a layout key, or "" for a line's. */
export function objectKind(key: string): SkyObjectKindId | "" {
  if (key === INTAKE_KEY) return "intake";
  if (key === PALETTE_KEY) return "palette";
  if (key === BEHAVIORS_KEY) return "behaviors";
  if (key === LANDING_KEY) return "landing";
  if (isPersonKey(key)) return "person";
  if (isDeviceKey(key)) return "device";
  if (isNoteKey(key)) return "note";
  return "";
}
export const isObjectKey = (key: string) => objectKind(key) !== "";

/** What decides which objects are on the board (the view's data, known
 * before anything is drawn): `skyObjects(data)` reads this. */
export interface SkyObjectData {
  mode: SkyMode;
  narrow: boolean;
  intakeOn: boolean;
  paletteOn: boolean;
  /** Peer links (people blocks), the other devices (device blocks), the notes. */
  links: PeerLink[];
  devices: Device[];
  notes: SkyNote[];
  /** The Behaviors board is shown (components/flights/BehaviorsBoard
   * `useBehaviorsBoard().on`); hidden, it is not on the board at all. */
  behaviorsOn?: boolean;
}

/** What the Behaviors board draws (`useBehaviorsBoard`): its rows, the row
 * open into its card, and its ×. */
export interface SkyBehaviorsView {
  rows: TestPlan[];
  expanded: string;
  setExpanded(id: string): void;
  hide(): void;
}

/** What the view gives every object when it is drawn: the data above and
 * the handlers that reach the sky (SkyView builds one per render). */
export interface SkyObjectView extends SkyObjectData {
  rows: Instance[];
  plans: FlightPlan[];
  /** The intake lane and the palette are drawn by the view (their JSX is
   * pinned there); the registry only places them. */
  renderIntake(host: SkyObjectHost): ReactNode;
  renderPalette(host: SkyObjectHost): ReactNode;
  flashLine(id: string): void;
  placeGrounded(d: CatalogDrop, near: "shelf" | "palette"): void;
  newHere(): void;
  onShelfDragStart(e: ReactDragEvent): void;
  onShelfDragEnd(): void;
  /** The landing strip's data (null until it loads) and its error. */
  landing: LandingData | null;
  landingError: string;
  /** A session line dropped on a block: the block asks (its inline
   * confirm) — the dropped title per block key, "" when nothing is asked. */
  dropped: Record<string, string>;
  /** The block's answer: done (the share / move went through) or cancelled. */
  answerDrop(key: string): void;
  /** The note being edited (its id), and the note handlers. */
  editingNote: string;
  setEditingNote(id: string): void;
  changeNote(id: string, patch: Partial<SkyNote>): void;
  removeNote(id: string): void;
  /** The Behaviors board's data (absent: no board). */
  behaviors?: SkyBehaviorsView;
}

/** What the sky gives one object when it draws it. */
export interface SkyObjectHost {
  key: string;
  zoom: number;
  lod: Lod;
  layoutEntry: StoredLine | undefined;
  pos: Pos;
  draggable: boolean;
  selected: boolean;
  dragging: boolean;
  /** A dragged line is over it (it lights as a drop target). */
  target: boolean;
  onHeadDown(e: ReactPointerEvent): void;
  onResize(w: number, h: number): void;
  onMeasure(w: number, h: number): void;
  onLayout(patch: Partial<SkyLineLayout>): void;
  view: SkyObjectView;
  /** The kind's data for this object (a link, a device, a note). */
  data?: PeerLink | Device | SkyNote;
}

/** One kind of sky object, in the registry. */
export interface SkyObjectDef {
  /** The layout key, or the key prefix of a kind with many objects. */
  key: string;
  kind: SkyObjectKindId;
  place: SkyPlace;
  min: Size;
  max: Size;
  /** The wrapper's class on the plane. The intake lane's and the palette's
   * are the round 12/13 names (Sky.css styles them). */
  className: string;
  /** A session line may be dropped on it (a person / device block). */
  accepts?: "session";
  /** The frame the owner dragged it to (its layout entry's w / h, clamped),
   * null when it sizes itself. */
  frame(stored: StoredLine | undefined): Size | null;
  /** The room it takes: its frame, else its measure, else a guess. */
  size(stored: StoredLine | undefined, measured: Size | undefined): Size;
  /** Which objects of this kind are on the board, in order. */
  on(data: SkyObjectData): { key: string; data?: PeerLink | Device | SkyNote }[];
  render(host: SkyObjectHost): ReactNode;
}

const INTAKE: SkyObjectDef = {
  key: INTAKE_KEY,
  kind: "intake",
  place: "left",
  min: { w: INTAKE_MIN_W, h: INTAKE_MIN_H },
  max: { w: INTAKE_MAX_W, h: INTAKE_MAX_H },
  className: "sky-intake",
  frame: (stored) => objectFrame(stored, { w: INTAKE_MIN_W, h: INTAKE_MIN_H }, { w: INTAKE_MAX_W, h: INTAKE_MAX_H }),
  // Never under the folded width: unfolding the rail never runs into a line.
  size: (stored, measured) => {
    const b = intakeSize(stored, measured);
    return { w: Math.max(b.w, INTAKE_W), h: b.h };
  },
  on: (view) => (view.intakeOn ? [{ key: INTAKE_KEY }] : []),
  render: (host) => host.view.renderIntake(host),
};

const PALETTE: SkyObjectDef = {
  key: PALETTE_KEY,
  kind: "palette",
  place: "left",
  min: PALETTE_MIN,
  max: PALETTE_MAX,
  className: "sky-palette-slot",
  frame: (stored) => objectFrame(stored, PALETTE_MIN, PALETTE_MAX),
  size: (stored, measured) => objectFrame(stored, PALETTE_MIN, PALETTE_MAX) || measured || { w: PALETTE_W, h: PALETTE_H },
  on: (view) => (view.paletteOn ? [{ key: PALETTE_KEY }] : []),
  render: (host) => host.view.renderPalette(host),
};

const LANDING: SkyObjectDef = {
  key: LANDING_KEY,
  kind: "landing",
  place: "right",
  min: LANDING_MIN,
  max: LANDING_MAX,
  className: "sky-object sky-object-landing",
  frame: (stored) => objectFrame(stored, LANDING_MIN, LANDING_MAX),
  size: (stored, measured) => objectFrame(stored, LANDING_MIN, LANDING_MAX) || (measured && measured.w > 0 ? measured : { w: LANDING_W, h: LANDING_H }),
  // Not in This session: one line, no strip.
  on: (view) => (view.mode === "one" || view.narrow ? [] : [{ key: LANDING_KEY }]),
  render: (host) =>
    createElement(LandingStrip, {
      data: host.view.landing,
      error: host.view.landingError,
      width: host.layoutEntry ? (objectFrame(host.layoutEntry, LANDING_MIN, LANDING_MAX)?.w ?? undefined) : undefined,
      height: host.layoutEntry ? (objectFrame(host.layoutEntry, LANDING_MIN, LANDING_MAX)?.h ?? undefined) : undefined,
      zoom: host.zoom,
      open: !!host.layoutEntry?.open,
      onToggleOpen: () => host.onLayout({ open: !host.layoutEntry?.open }),
      onHeadDown: host.onHeadDown,
      draggable: host.draggable,
      onResize: host.onResize,
      onMeasure: host.onMeasure,
    }),
};

/** The Behaviors board (docs/behaviors.md §3): the behaviors with no session
 * frame in the Sky, at the top of the right column. Not in This session or
 * on a phone; else only while shown (`behaviorsOn`). */
const BEHAVIORS: SkyObjectDef = {
  key: BEHAVIORS_KEY,
  kind: "behaviors",
  place: "right",
  min: BEHAVIORS_MIN,
  max: BEHAVIORS_MAX,
  className: "sky-object sky-object-behaviors",
  frame: (stored) => objectFrame(stored, BEHAVIORS_MIN, BEHAVIORS_MAX),
  size: (stored, measured) => objectFrame(stored, BEHAVIORS_MIN, BEHAVIORS_MAX) || (measured && measured.w > 0 ? measured : { w: BEHAVIORS_W, h: BEHAVIORS_H }),
  on: (view) => (view.mode === "one" || view.narrow || !view.behaviorsOn ? [] : [{ key: BEHAVIORS_KEY }]),
  render: (host) => {
    const b = host.view.behaviors;
    const frame = host.layoutEntry ? objectFrame(host.layoutEntry, BEHAVIORS_MIN, BEHAVIORS_MAX) : null;
    return createElement(BehaviorsBoard, {
      plans: b?.rows || [],
      expanded: b?.expanded || "",
      onExpand: (id: string) => b?.setExpanded(id),
      onHide: b?.hide,
      width: frame?.w,
      height: frame?.h,
      zoom: host.zoom,
      onHeadDown: host.onHeadDown,
      draggable: host.draggable,
      onResize: host.onResize,
      onMeasure: host.onMeasure,
    });
  },
};

const DEVICE: SkyObjectDef = {
  key: "device:",
  kind: "device",
  place: "right",
  min: { w: BLOCK_W, h: BLOCK_H },
  max: { w: BLOCK_W, h: 400 },
  className: "sky-object sky-object-block sky-object-device",
  accepts: "session",
  frame: () => null,
  size: (_s, measured) => (measured && measured.w > 0 ? measured : { w: BLOCK_W, h: BLOCK_H }),
  on: (view) => (view.mode === "one" || view.narrow ? [] : view.devices.map((d) => ({ key: deviceKey(d.device), data: d }))),
  render: (host) =>
    createElement(DeviceBlock, {
      device: host.data as Device,
      dropped: host.view.dropped[host.key] || "",
      onDone: () => host.view.answerDrop(host.key),
      target: host.target,
      onHeadDown: host.onHeadDown,
      draggable: host.draggable,
      onMeasure: host.onMeasure,
    }),
};

const PERSON: SkyObjectDef = {
  key: "person:",
  kind: "person",
  place: "right",
  min: { w: BLOCK_W, h: BLOCK_H },
  max: { w: BLOCK_W, h: 400 },
  className: "sky-object sky-object-block sky-object-person",
  accepts: "session",
  frame: () => null,
  size: (_s, measured) => (measured && measured.w > 0 ? measured : { w: BLOCK_W, h: BLOCK_H }),
  on: (view) => (view.mode === "one" || view.narrow ? [] : view.links.map((l) => ({ key: personKey(l.link_id), data: l }))),
  render: (host) =>
    createElement(PersonBlock, {
      link: host.data as PeerLink,
      rows: host.view.rows,
      dropped: host.view.dropped[host.key] || "",
      onDone: () => host.view.answerDrop(host.key),
      target: host.target,
      onHeadDown: host.onHeadDown,
      draggable: host.draggable,
      onMeasure: host.onMeasure,
    }),
};

const NOTE: SkyObjectDef = {
  key: "note:",
  kind: "note",
  place: "free",
  min: NOTE_MIN,
  max: NOTE_MAX,
  className: "sky-object sky-object-note",
  frame: (stored) => objectFrame(stored, NOTE_MIN, NOTE_MAX),
  size: (stored) => objectFrame(stored, NOTE_MIN, NOTE_MAX) || { w: NOTE_W, h: NOTE_H },
  on: (view) => (view.mode === "one" || view.narrow ? [] : view.notes.map((n) => ({ key: noteKey(n.id), data: n }))),
  render: (host) => {
    const note = host.data as SkyNote;
    return createElement(StickyNote, {
      note,
      zoom: host.zoom,
      editing: host.view.editingNote === note.id,
      onEdit: (on: boolean) => host.view.setEditingNote(on ? note.id : ""),
      onChange: (text: string) => host.view.changeNote(note.id, { text }),
      onResize: (w: number, h: number) => host.view.changeNote(note.id, { w, h }),
      onRemove: () => host.view.removeNote(note.id),
      onHeadDown: host.onHeadDown,
      draggable: host.draggable,
      onMeasure: host.onMeasure,
      attached: !!note.line,
    });
  },
};

/** The registry: every kind of sky object, in placement order — the left
 * ones first (the lane, then the palette), then the right column (the
 * Behaviors board, the strip, the devices, the people), then the free ones
 * (notes). */
export const SKY_OBJECTS: SkyObjectDef[] = [INTAKE, PALETTE, BEHAVIORS, LANDING, DEVICE, PERSON, NOTE];

/** One object on the board: its kind's def, under its own key. */
export interface SkyObjectItem {
  key: string;
  def: SkyObjectDef;
  data?: PeerLink | Device | SkyNote;
}

/** The objects on the board now, in placement order. */
export function skyObjects(data: SkyObjectData): SkyObjectItem[] {
  const out: SkyObjectItem[] = [];
  for (const def of SKY_OBJECTS) for (const o of def.on(data)) out.push({ key: o.key, def, data: o.data });
  return out;
}

/** A sticky note's frame: its own w / h (the server's), clamped. */
export function noteSize(note: Pick<SkyNote, "w" | "h">): Size {
  return objectFrame({ w: note.w, h: note.h }, NOTE_MIN, NOTE_MAX) || { w: NOTE_W, h: NOTE_H };
}

/** The topmost item whose rect holds `point` (plane px), or "". Lines come
 * in drawing order, so the last match wins; nothing is excluded — the caller
 * passes only what may be dropped on. */
export function hitAt(point: Pos, items: readonly { id: string; rect: Rect }[]): string {
  for (let i = items.length - 1; i >= 0; i--) {
    const { id, rect: r } = items[i];
    if (point.x >= r.x && point.x <= r.x + r.w && point.y >= r.y && point.y <= r.y + r.h) return id;
  }
  return "";
}

/** Where a note sits: its own x / y, or — attached — relative to its line's
 * spot (it moves with the line). A line that is gone leaves the note where
 * the line was last known, else at its own numbers. */
export function notePos(note: Pick<SkyNote, "x" | "y" | "line">, lineAt: (id: string) => Pos | null): Pos {
  if (note.line) {
    const at = lineAt(note.line);
    if (at) return { x: at.x + note.x, y: at.y + note.y };
  }
  return { x: note.x, y: note.y };
}

/** The note after a drop at `at` (plane px): on a line, attached to it with
 * its offset from the line's spot; on empty sky, free at `at`. */
export function noteDropped(note: SkyNote, at: Pos, line: string, lineAt: Pos | null): SkyNote {
  if (line && lineAt) return { ...note, line, x: at.x - lineAt.x, y: at.y - lineAt.y };
  return { ...note, line: "", x: at.x, y: at.y };
}

/** A new note's id. */
export function newNoteId(): string {
  const rand = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID().slice(0, 8) : Math.random().toString(36).slice(2, 10);
  return "n_" + Date.now().toString(36) + rand;
}

/** The landing strip's data: polled every minute (the server caches it as
 * long), only while the strip is on the board. */
export function useLandingData(enabled: boolean) {
  return useQuery({ queryKey: ["sky-landing"], queryFn: fetchLanding, refetchInterval: 60_000, retry: false, enabled });
}

/** The notes as stored on the server: read once (the view keeps them from
 * there, and saves its own changes). */
export function useSkyNotesQuery() {
  return useQuery({ queryKey: ["sky-notes"], queryFn: fetchSkyNotes, retry: false, staleTime: Infinity });
}

/** The wrapper class of an object on the plane: its kind's, plus the drag,
 * selection and drop-target states. */
export function objectClass(def: Pick<SkyObjectDef, "className">, state: { dragging?: boolean; selected?: boolean; target?: boolean }): string {
  return def.className + (state.dragging ? " is-dragging" : "") + (state.selected ? " is-selected" : "") + (state.target ? " is-merge-target" : "");
}
