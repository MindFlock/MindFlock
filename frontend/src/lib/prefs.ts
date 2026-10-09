/** Preferences that follow the person (settings group `prefs`, synced across
 * "Your devices").
 *
 * These used to live only in this browser's localStorage: a keymap, saved
 * prompts, the theme, the sidebar's bars, break and idle-flock reminders. A
 * second computer started from nothing, and so did the desktop app next to a
 * browser tab on the same machine. Now each mapped key is mirrored to the
 * server's `prefs` group (GET/POST /api/prefs), which settings sync carries to
 * every one of your devices.
 *
 * localStorage stays the synchronous cache — every reader in the app still
 * reads it at startup exactly as before, so nothing waits on the network and
 * an older server without /api/prefs changes nothing. This module only adds:
 *  - the mapping (`PREF_MAP`) and its parse/serialize rules,
 *  - `planBoot`: what to upload and what to adopt when the page loads,
 *  - `notePrefWrite`: a debounced POST after any local write of a mapped key,
 *    with the field kept "dirty" in localStorage until the server confirms
 *    it (retried with backoff, sent on page hide, uploaded first on the next
 *    load) — a write is never lost to a restarting server or a quick reload.
 *
 * Pure apart from the write-back's timers; applying adopted values to the
 * live UI is lib/prefsSync.ts's job (it imports the store, which imports
 * this). */

import { api } from "../api/client";

/** The `prefs` settings group as GET /api/prefs returns it (every key present,
 * defaults filled in). */
export interface Prefs {
  keymap: Record<string, unknown>;
  prompt_presets: Array<{ name: string; prompt: string }>;
  theme: string;
  diff_mode: string;
  diff_base: string;
  hidden_bars: string[] | null;
  bar_order: string[];
  reduce_motion: boolean | null;
  break_on: boolean | null;
  break_every: number | null;
  idle_flock: boolean | null;
  idle_after: number | null;
  hints: boolean | null;
}

export type PrefField = keyof Prefs;

/** What a field holds when nobody has set it — the server's defaults. */
export const PREF_DEFAULTS: Prefs = {
  keymap: {},
  prompt_presets: [],
  theme: "",
  diff_mode: "",
  diff_base: "",
  hidden_bars: null,
  bar_order: [],
  reduce_motion: null,
  break_on: null,
  break_every: null,
  idle_flock: null,
  idle_after: null,
  hints: null,
};

type Kind = "json-object" | "json-list" | "json-bool" | "json-number" | "raw";

interface PrefKey {
  /** The localStorage key — unchanged from before, so nothing migrates. */
  ls: string;
  field: PrefField;
  kind: Kind;
  /** For "raw" keys: the only values that mean something (anything else in
   * storage is treated as absent). */
  values?: string[];
}

/** localStorage key ↔ prefs field. The raw ones were always stored as bare
 * strings (`cs_theme` is read by index.html's pre-paint script as one), the
 * rest as JSON. */
export const PREF_MAP: PrefKey[] = [
  { ls: "mf_keymap", field: "keymap", kind: "json-object" },
  { ls: "mindflock.prompt_presets", field: "prompt_presets", kind: "json-list" },
  { ls: "cs_theme", field: "theme", kind: "raw", values: ["light", "dark"] },
  { ls: "cs_diffmode", field: "diff_mode", kind: "raw", values: ["split", "unified"] },
  { ls: "mf_diffbase", field: "diff_base", kind: "raw", values: ["fork", "head"] },
  { ls: "mf_hiddenbars", field: "hidden_bars", kind: "json-list" },
  { ls: "mf_barorder", field: "bar_order", kind: "json-list" },
  { ls: "mf_reduce_motion", field: "reduce_motion", kind: "json-bool" },
  { ls: "mf_break_on", field: "break_on", kind: "json-bool" },
  { ls: "mf_break_every", field: "break_every", kind: "json-number" },
  { ls: "mf_idle_flock", field: "idle_flock", kind: "json-bool" },
  { ls: "mf_idle_after", field: "idle_after", kind: "json-number" },
  { ls: "mf_hints", field: "hints", kind: "json-bool" },
];

const BY_LS = new Map(PREF_MAP.map((p) => [p.ls, p]));
const BY_FIELD = new Map(PREF_MAP.map((p) => [p.field, p]));

/** Fired on document after the theme was changed from outside the top bar
 * (an adopted pref), so its toggle's own state follows. */
export const THEME_CHANGED = "mf-theme-changed";

/** Set once this browser has reconciled with the server (see planBoot). */
export const SEEDED_KEY = "mf_prefs_seeded";

export interface KV {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
  removeItem(k: string): void;
}

function storage(): KV | null {
  try {
    return (globalThis as { localStorage?: KV }).localStorage || null;
  } catch {
    return null;
  }
}

export function prefFieldFor(lsKey: string): PrefField | null {
  return BY_LS.get(lsKey)?.field ?? null;
}

export function lsKeyFor(field: PrefField): string {
  return BY_FIELD.get(field)!.ls;
}

/** A value of the right shape for its kind, or undefined. */
function coerce(p: PrefKey, v: unknown): unknown {
  switch (p.kind) {
    case "json-object":
      return v && typeof v === "object" && !Array.isArray(v) ? v : undefined;
    case "json-list":
      return Array.isArray(v) ? v : undefined;
    case "json-bool":
      return typeof v === "boolean" ? v : undefined;
    case "json-number":
      return typeof v === "number" && isFinite(v) ? v : undefined;
    case "raw":
      return typeof v === "string" && (!p.values || p.values.includes(v)) ? v : undefined;
  }
}

/** One field's value as this browser has it, or undefined when it has none
 * (never written, or unparseable — same as the app's own readers treat it). */
export function readLocal(field: PrefField, kv: KV | null = storage()): unknown {
  const p = BY_FIELD.get(field);
  if (!p || !kv) return undefined;
  let raw: string | null = null;
  try {
    raw = kv.getItem(p.ls);
  } catch {
    return undefined;
  }
  if (raw === null) return undefined;
  if (p.kind === "raw") return coerce(p, raw);
  try {
    return coerce(p, JSON.parse(raw));
  } catch {
    return undefined;
  }
}

/** Every mapped field this browser holds. */
export function readAllLocal(kv: KV | null = storage()): Partial<Prefs> {
  const out: Partial<Record<PrefField, unknown>> = {};
  for (const p of PREF_MAP) {
    const v = readLocal(p.field, kv);
    if (v !== undefined) out[p.field] = v;
  }
  return out as Partial<Prefs>;
}

/** Store a field in localStorage the way its readers expect it; an unset
 * value removes the key, so the reader falls back to its own default (which
 * is what an unset server field means). */
export function writeLocal(field: PrefField, value: unknown, kv: KV | null = storage()): void {
  const p = BY_FIELD.get(field);
  if (!p || !kv) return;
  try {
    if (isUnset(field, value)) kv.removeItem(p.ls);
    else kv.setItem(p.ls, p.kind === "raw" ? String(value) : JSON.stringify(value));
  } catch {
    /* storage unavailable */
  }
}

/** Whether a value means "nobody chose anything" for its field. A keymap
 * with no rebinds and no chord changes is the defaults, however it is
 * spelled; an EMPTY hidden-bars list is a real choice (every bar shown), so
 * only null is unset there. */
export function isUnset(field: PrefField, v: unknown): boolean {
  if (v === undefined || v === null) return true;
  const p = BY_FIELD.get(field);
  if (!p) return true;
  if (coerce(p, v) === undefined) return true;
  if (field === "keymap") {
    const km = v as { keys?: object; chords?: object };
    const empty = (o: unknown) => !o || typeof o !== "object" || !Object.keys(o).length;
    return empty(km.keys) && empty(km.chords);
  }
  if (field === "hidden_bars") return false;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "string") return v === "";
  return false;
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export interface BootPlan {
  /** Fields to POST: this browser had them and the server didn't, a merge
   * of both (prompt presets), or a local write that never reached the
   * server (`dirty`). */
  upload: Partial<Prefs>;
  /** Fields to adopt locally (an unset value clears the local key). */
  apply: Partial<Prefs>;
}

type PresetList = Prefs["prompt_presets"];

/** A preset IS its name, ignoring case and surrounding spaces — the server's
 * rule (config/settings.py `_prompt_presets`). */
export function presetKey(name: string): string {
  return String(name || "").trim().toLowerCase();
}

/** One preset per name, names trimmed. A repeated name keeps the NEWEST
 * entry (the later one: every save appends), at the position of the first;
 * `dropped` lists the older names that lost, so the caller can say so
 * rather than the server silently keeping the first. */
export function normalizePresets(list: PresetList): { list: PresetList; dropped: string[] } {
  const at = new Map<string, number>();
  const out: PresetList = [];
  const dropped: string[] = [];
  for (const p of Array.isArray(list) ? list : []) {
    if (!p || typeof p.name !== "string") continue;
    const name = p.name.trim();
    if (!name) continue;
    const entry = { ...p, name };
    const k = presetKey(name);
    const i = at.get(k);
    if (i === undefined) {
      at.set(k, out.length);
      out.push(entry);
    } else {
      dropped.push(out[i].name);
      out[i] = entry;
    }
  }
  return { list: out, dropped };
}

/** The server's list with this browser's merged in: same name → this
 * browser's entry (in the server's position), names the server lacks are
 * appended. Used once, by a browser that never reconciled — its saved
 * prompts are the person's own writing, not a cache to overwrite. */
export function mergePresets(server: PresetList, local: PresetList): PresetList {
  const mine = new Map<string, PresetList[number]>();
  for (const p of normalizePresets(local).list) mine.set(presetKey(p.name), p);
  const out: PresetList = [];
  const seen = new Set<string>();
  for (const p of normalizePresets(server).list) {
    const k = presetKey(p.name);
    seen.add(k);
    out.push(mine.get(k) || p);
  }
  for (const [k, p] of mine) if (!seen.has(k)) out.push(p);
  return out;
}

/** Reconcile this browser with the server's prefs at page load (and on every
 * re-pull after settings sync brought something in).
 *
 * A field in `dirty` was written here and never confirmed by the server (the
 * POST failed, or the page closed inside the debounce): it is the newest
 * value anywhere, so it is uploaded and never overwritten or cleared.
 *
 * Otherwise a set server field wins: it is the person's latest choice, from
 * here or from another of their devices — except saved prompts in a browser
 * that never reconciled, which are merged by name (this browser's own entry
 * wins on a shared name) and the union uploaded. An unset server field is
 * ambiguous, which is what `seeded` resolves:
 *  - before this browser ever reconciled, it means "the server hasn't heard
 *    of it yet" — upload what this browser has (once), so the first device
 *    to update keeps its setup and the others then take it;
 *  - after, it means someone cleared it — clear it here too. Without that, a
 *    second browser on the same server (the desktop app next to a tab) would
 *    re-upload the prompts you just deleted on the first. */
export function planBoot(
  server: Partial<Prefs>,
  local: Partial<Prefs>,
  seeded: boolean,
  dirty: ReadonlySet<PrefField> = new Set()
): BootPlan {
  const upload: Partial<Record<PrefField, unknown>> = {};
  const apply: Partial<Record<PrefField, unknown>> = {};
  for (const p of PREF_MAP) {
    const f = p.field;
    const s = server[f];
    const l = local[f];
    const localSet = !isUnset(f, l);
    if (dirty.has(f)) {
      // null clears the server field — what a removed local key means.
      if (!same(s, l)) upload[f] = l === undefined ? null : l;
    } else if (!isUnset(f, s)) {
      if (f === "prompt_presets" && !seeded && localSet) {
        const merged = mergePresets(s as PresetList, l as PresetList);
        if (!same(merged, s)) upload[f] = merged;
        if (!same(merged, l)) apply[f] = merged;
      } else if (!localSet || !same(s, l)) apply[f] = s;
    } else if (localSet) {
      if (seeded) apply[f] = PREF_DEFAULTS[f];
      else upload[f] = l;
    }
  }
  return { upload: upload as Partial<Prefs>, apply: apply as Partial<Prefs> };
}

// --- Writing back -----------------------------------------------------------
//
// A local write is "dirty" until the server confirms it: the field is kept
// in localStorage under DIRTY_KEY (so a reload, a crash or a closed desktop
// window can't lose it — the next load uploads it before taking anything
// from the server) and is only cleared once its POST succeeds. Tabs of one
// browser share localStorage, values and dirty marks alike, so whichever
// sends it first is fine.

const DEBOUNCE_MS = 500;
const RETRY_MIN_MS = 5000;
const RETRY_MAX_MS = 120000;

/** localStorage key: the fields written here that the server hasn't
 * confirmed yet, as a JSON list. */
export const DIRTY_KEY = "mf_prefs_dirty";

const pending = new Set<PrefField>();
/** Bumped on every local write of a field: a POST that was already in
 * flight when the field changed again doesn't clear the newer write. */
const gen = new Map<PrefField, number>();
let timer: ReturnType<typeof setTimeout> | null = null;
let retryMs = 0;
let suspended = 0;
let sender: (body: Partial<Prefs>) => Promise<unknown> = (body) =>
  api("/api/prefs", { json: body });
let beacon: (body: Partial<Prefs>) => Promise<unknown> = (body) =>
  fetch("/api/prefs", {
    method: "POST",
    keepalive: true,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => {
    if (!r.ok) throw new PrefPostError(r.status);
    return r;
  });

class PrefPostError extends Error {
  status: number;
  constructor(status: number) {
    super("/api/prefs -> " + status);
    this.status = status;
  }
}

/** Tests swap the POST; returns the previous one. */
export function setPrefSender(fn: (body: Partial<Prefs>) => Promise<unknown>) {
  const prev = sender;
  sender = fn;
  return prev;
}

/** Tests swap the page-hide POST (fetch with keepalive); returns the previous. */
export function setPrefBeacon(fn: (body: Partial<Prefs>) => Promise<unknown>) {
  const prev = beacon;
  beacon = fn;
  return prev;
}

const FIELDS = new Set<string>(PREF_MAP.map((p) => p.field));

/** The fields written here that the server hasn't confirmed. */
export function dirtyFields(kv: KV | null = storage()): Set<PrefField> {
  const out = new Set<PrefField>();
  if (!kv) return out;
  try {
    const arr = JSON.parse(kv.getItem(DIRTY_KEY) || "[]");
    if (Array.isArray(arr)) for (const f of arr) if (FIELDS.has(f)) out.add(f as PrefField);
  } catch {
    /* unreadable: nothing is known dirty */
  }
  return out;
}

function saveDirty(set: Set<PrefField>, kv: KV | null) {
  if (!kv) return;
  try {
    if (set.size) kv.setItem(DIRTY_KEY, JSON.stringify([...set].sort()));
    else kv.removeItem(DIRTY_KEY);
  } catch {
    /* storage unavailable */
  }
}

function markDirty(field: PrefField, kv: KV | null) {
  const d = dirtyFields(kv);
  if (d.has(field)) return;
  d.add(field);
  saveDirty(d, kv);
}

/** The server confirmed these fields' values as of `sentGen`: clear the
 * dirty mark of each one that hasn't been written again since. */
export function settleFields(
  fields: Iterable<PrefField>,
  sentGen: Map<PrefField, number> | null = null,
  kv: KV | null = storage()
) {
  const d = dirtyFields(kv);
  let changed = false;
  for (const f of fields) {
    if (sentGen && (gen.get(f) || 0) !== (sentGen.get(f) || 0)) continue;
    if (pending.has(f)) continue;
    if (d.delete(f)) changed = true;
  }
  if (changed) saveDirty(d, kv);
}

/** Snapshot of each field's write generation (pass to settleFields). */
export function writeGens(fields: Iterable<PrefField>): Map<PrefField, number> {
  const m = new Map<PrefField, number>();
  for (const f of fields) m.set(f, gen.get(f) || 0);
  return m;
}

/** Run `fn` without echoing its localStorage writes back to the server —
 * used while adopting the server's own values. */
export function withoutEcho<T>(fn: () => T): T {
  suspended++;
  try {
    return fn();
  } finally {
    suspended--;
  }
}

/** Fields written locally that haven't reached the server yet (debouncing,
 * in flight, or failed). A pull that lands meanwhile must not overwrite them
 * with the older server copy. */
export function pendingFields(kv: KV | null = storage()): Set<PrefField> {
  const out = dirtyFields(kv);
  for (const f of pending) out.add(f);
  return out;
}

/** Call after writing a localStorage key: if it is a mapped pref, mark it
 * dirty and POST its current value (batched with any other pref written in
 * the next 500 ms). */
export function notePrefWrite(lsKey: string, kv: KV | null = storage()): void {
  const field = prefFieldFor(lsKey);
  if (!field || suspended) return;
  pending.add(field);
  gen.set(field, (gen.get(field) || 0) + 1);
  markDirty(field, kv);
  retryMs = 0;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => void flushPrefWrites(), DEBOUNCE_MS);
}

/** A rejection the server meant (a 4xx other than auth, e.g. 404 on an
 * older server without /api/prefs, or a value it refuses): retrying sends
 * the same thing again, so the field stops being dirty. Anything else
 * (network down, server restarting, 5xx) is worth another try — and so is a
 * 409: settings.json can't be read right now, so the server kept nothing,
 * and dropping the dirty mark would let the first pull after the file is
 * fixed put the old value (or the defaults) back over this change. */
function permanent(e: unknown): boolean {
  const st = (e as { status?: unknown })?.status;
  return (
    typeof st === "number" && st >= 400 && st < 500 && st !== 401 && st !== 408 && st !== 409 && st !== 429
  );
}

function bodyFor(fields: Iterable<PrefField>, kv: KV | null): Partial<Prefs> {
  const body: Partial<Record<PrefField, unknown>> = {};
  for (const f of fields) {
    const v = readLocal(f, kv);
    // null clears the server field — what a removed local key means.
    body[f] = v === undefined ? null : v;
  }
  return body as Partial<Prefs>;
}

/** Send what's unsent now: the debounce's tail, and every field still dirty
 * from an earlier failure or an earlier page (tests call it directly). A
 * failed send keeps the fields dirty and retries with backoff. */
export async function flushPrefWrites(kv: KV | null = storage()): Promise<void> {
  if (timer) clearTimeout(timer);
  timer = null;
  const fields = new Set<PrefField>([...pending, ...dirtyFields(kv)]);
  pending.clear();
  if (!fields.size) return;
  const sentGen = writeGens(fields);
  try {
    await sender(bodyFor(fields, kv));
  } catch (e) {
    if (permanent(e)) {
      settleFields(fields, sentGen, kv);
      return;
    }
    // Still dirty (in localStorage): the next load, the next pull or this
    // retry sends it.
    retryMs = Math.min(RETRY_MAX_MS, retryMs ? retryMs * 2 : RETRY_MIN_MS);
    if (!timer) timer = setTimeout(() => void flushPrefWrites(), retryMs);
    return;
  }
  retryMs = 0;
  settleFields(fields, sentGen, kv);
}

/** The page is going away (reload, tab closed, desktop window closed) or
 * hidden: send every unsent field now with a request that outlives the page
 * (fetch keepalive). If it doesn't make it, the fields stay dirty and the
 * next load uploads them first. */
export function flushPrefsOnHide(kv: KV | null = storage()): void {
  if (timer) clearTimeout(timer);
  timer = null;
  const fields = new Set<PrefField>([...pending, ...dirtyFields(kv)]);
  pending.clear();
  if (!fields.size) return;
  const sentGen = writeGens(fields);
  beacon(bodyFor(fields, kv)).then(
    () => settleFields(fields, sentGen, kv),
    (e) => {
      if (permanent(e)) settleFields(fields, sentGen, kv);
    }
  );
}
