/** Extension runtime host (Addon API v3) — React-free.
 *
 * Owns the per-extension records: activation (lazy, promise-cached), the
 * command registry, and — the part that makes extensions feel native — the
 * KEEP-ALIVE surface containers. Like lib/terminals' registry, every dialog or
 * pane body is a detached <div> owned here; a React component adopts it via
 * appendChild on mount and detaches it (without disposing) on unmount, so
 * typed SQL and dirty grid cells survive grid drags and row reflows. A surface
 * instance is disposed only when it is explicitly closed, or when its whole
 * extension is deactivated.
 *
 * React components subscribe through subscribeHost()/hostVersion() (a plain
 * external-store version counter) and read state through the *View accessors —
 * nothing in here imports React. */

import { api } from "../api/client";
import { toast } from "../lib/toast";
import { useUi } from "../state/store";
import type {
  ApiOptions,
  Disposable,
  ExtensionApi,
  ExtensionInfo,
  ExtensionModule,
  ExtensionSpec,
  SurfaceHost,
  SurfaceRenderer,
} from "./types";

/** The host's API level. `api_version` in a manifest is the MINIMUM level the
 * extension needs; bump this on every addition to the ExtensionApi surface. */
export const HOST_API_VERSION = 1;

declare global {
  interface Window {
    /** The module-registration alternative to a default export: an extension
     * module may run `window.mindflockExtensions["<id>"] = {activate}` at
     * import time instead of default-exporting. */
    mindflockExtensions?: Record<string, ExtensionModule | undefined>;
  }
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

export type ExtStatus = "idle" | "loading" | "active" | "error";

/** One live surface instance. `epoch` is the staleness token: captured at
 * open, compared against the live registry after every await, so a mount that
 * resumes after a slow module load can tell the instance was closed (Escape
 * mid-load) and dispose itself instead of rendering into limbo. */
interface SurfaceRuntime {
  kind: "pane" | "dialog";
  /** The keep-alive root the extension renders into. */
  el: HTMLElement;
  surfaceId: string;
  ref?: string;
  ctx?: unknown;
  /** Renderer has run (once per instance, at first mount). */
  started: boolean;
  /** A failure pinned to this instance (renderer threw, surface missing). */
  error?: string;
  cleanup?: Disposable | void;
  disposed?: boolean;
  epoch: number;
  /** Dialog only: live chrome title (panes keep theirs in the UI store). */
  title?: string;
}

interface DialogRuntime extends SurfaceRuntime {
  kind: "dialog";
  /** The full dialogTarget string this body belongs to. */
  target: string;
}

interface ExtRecord {
  ext: ExtensionInfo;
  status: ExtStatus;
  error?: string;
  api?: ExtensionApi;
  activation?: Promise<void>;
  /** True only while the extension's own activate() is on the stack — lets
   * commands.run of a not-yet-registered command fail loudly instead of
   * deadlocking on its own activation promise. */
  activating?: boolean;
  commands: Map<string, (...args: unknown[]) => void>;
  surfaces: Map<string, SurfaceRenderer>;
  disposables: Disposable[];
  panes: Map<string, SurfaceRuntime>;
  dialog?: DialogRuntime;
  style?: HTMLStyleElement;
  /** Monotonic per-surface counters for host-minted multi refs. */
  refCounters: Map<string, number>;
}

const records = new Map<string, ExtRecord>();

/** The current manifest snapshot (enabled extensions only), fed by
 * syncExtensions() from the useExtensions() query. */
let known = new Map<string, ExtensionInfo>();

/** Global staleness counter — every surface open takes the next token. */
let epochCounter = 0;

// --- Change notification (useSyncExternalStore-compatible) -----------------

let version = 0;
const listeners = new Set<() => void>();

export function subscribeHost(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function hostVersion(): number {
  return version;
}

function bump() {
  version++;
  for (const cb of listeners) cb();
}

// ---------------------------------------------------------------------------
// Target strings
// ---------------------------------------------------------------------------

/** "<extId>:<surfaceId>[:<ref>]" — the pane key and the dialogTarget format. */
export function buildTarget(extId: string, surfaceId: string, ref?: string): string {
  return extId + ":" + surfaceId + (ref ? ":" + ref : "");
}

export interface ParsedTarget {
  extId: string;
  surfaceId: string;
  ref?: string;
}

/** Inverse of buildTarget. indexOf slices, NOT split(":"): ext and surface ids
 * can't contain colons (id regex) but a ref is an opaque token that may — the
 * remainder after the second colon is the ref verbatim. */
export function parseTarget(target: string): ParsedTarget {
  const a = target.indexOf(":");
  if (a < 0) return { extId: target, surfaceId: "" };
  const b = target.indexOf(":", a + 1);
  if (b < 0) return { extId: target.slice(0, a), surfaceId: target.slice(a + 1) };
  return {
    extId: target.slice(0, a),
    surfaceId: target.slice(a + 1, b),
    ref: target.slice(b + 1),
  };
}

// ---------------------------------------------------------------------------
// Failure containment
// ---------------------------------------------------------------------------

/** Every extension-attributed error goes through here, so a broken extension
 * reads as "[extension dbclient] …" in the console instead of as a host bug. */
function extError(extId: string, msg: string, err?: unknown) {
  if (err !== undefined) console.error("[extension " + extId + "] " + msg, err);
  else console.error("[extension " + extId + "] " + msg);
}

function errText(err: unknown): string {
  return String((err as Error)?.message || err);
}

// ---------------------------------------------------------------------------
// Registry sync + activation
// ---------------------------------------------------------------------------

function getRecord(ext: ExtensionInfo): ExtRecord {
  let rec = records.get(ext.id);
  if (!rec) {
    rec = {
      ext,
      status: "idle",
      commands: new Map(),
      surfaces: new Map(),
      disposables: [],
      panes: new Map(),
      refCounters: new Map(),
    };
    records.set(ext.id, rec);
  }
  return rec;
}

/** Feed the host the derived enabled set (called from a React effect watching
 * useExtensions(), NOT from the Settings toggle click — so a disable made in
 * another tab lands here on the next manifest refetch). Deactivates every
 * record whose extension is now disabled or gone from the manifest. */
export function syncExtensions(list: ExtensionInfo[]) {
  known = new Map(list.filter((e) => e.enabled).map((e) => [e.id, e]));
  for (const id of [...records.keys()]) {
    if (!known.has(id)) deactivateExtension(id);
  }
  // Keep surviving records pointed at the fresh manifest objects (labels and
  // specs can change across a server restart + refetch).
  for (const [id, rec] of records) {
    const ext = known.get(id);
    if (ext) rec.ext = ext;
  }
}

function injectStylesheet(rec: ExtRecord, spec: ExtensionSpec) {
  if (rec.style || typeof document === "undefined") return;
  const dir = spec.module.slice(0, spec.module.lastIndexOf("/") + 1);
  const el = document.createElement("style");
  el.dataset.mfx = rec.ext.id;
  // layer(components): a sloppy extension selector loses to the theme layer
  // instead of beating the whole app.
  el.textContent = '@import url("' + dir + 'style.css") layer(components);';
  document.head.appendChild(el);
  rec.style = el;
}

/** Load + activate an extension's module. Promise-cached and idempotent: every
 * caller of the same extension awaits the same activation. A failure lands in
 * the record (status "error" + message, shown on Settings → Extensions) and
 * toasts once — it never throws to the caller. */
export function activateExtension(ext: ExtensionInfo): Promise<void> {
  const rec = getRecord(ext);
  if (rec.activation) return rec.activation;
  // A failed extension stays failed for the life of the page: re-enabling it
  // from Settings deactivates (deleting this record) and starts fresh.
  if (rec.status === "error") return Promise.resolve();
  rec.status = "loading";
  bump();
  rec.activation = (async () => {
    try {
      const spec = ext.extension;
      if ((spec.api_version || 1) > HOST_API_VERSION) {
        throw new Error(
          "needs host API level " + spec.api_version + " (this app provides " + HOST_API_VERSION + ")"
        );
      }
      if (spec.stylesheet) injectStylesheet(rec, spec);
      const mod = (await import(/* @vite-ignore */ spec.module)) as {
        default?: ExtensionModule;
      };
      const entry = mod?.default ?? window.mindflockExtensions?.[ext.id];
      if (!entry || typeof entry.activate !== "function") {
        throw new Error("module exports no activate()");
      }
      const apiObj = rec.api ?? (rec.api = makeApi(ext));
      // Flip to active BEFORE activate() runs: registrations made inside it
      // must land on a live record, and commands.run during activate takes the
      // registered-handler path instead of re-awaiting this same promise.
      rec.status = "active";
      rec.activating = true;
      try {
        await entry.activate(apiObj);
      } finally {
        rec.activating = false;
      }
      bump();
    } catch (err) {
      rec.status = "error";
      rec.error = errText(err);
      extError(ext.id, "activation failed", err);
      toast("Extension " + (ext.label || ext.id) + " failed: " + rec.error);
      // Registrations from the partial activate are dead weight — drain them
      // now so a half-activated extension can't keep half-working.
      drainRegistrations(rec);
      bump();
    }
  })();
  return rec.activation;
}

function drainRegistrations(rec: ExtRecord) {
  for (const d of rec.disposables.splice(0)) {
    try {
      d.dispose();
    } catch (err) {
      extError(rec.ext.id, "dispose failed", err);
    }
  }
  rec.commands.clear();
  rec.surfaces.clear();
}

/** Tear an extension down completely: drain its registrations, dispose every
 * pane/dialog body, close its windows in the store, drop its stylesheet, and
 * delete the record (so a re-enable starts from scratch). */
export function deactivateExtension(extId: string) {
  const rec = records.get(extId);
  if (!rec) return;
  // Delete first so anything reentrant (a dispose() that calls closePane)
  // finds no record and no-ops instead of recursing.
  records.delete(extId);
  drainRegistrations(rec);
  for (const [key, runtime] of rec.panes) {
    disposeRuntime(extId, runtime);
    useUi.getState().closeExtPane(key);
  }
  rec.panes.clear();
  if (rec.dialog) {
    const s = useUi.getState();
    if (s.openDialog === "extension" && s.dialogTarget === rec.dialog.target) s.closeDialog();
    disposeRuntime(extId, rec.dialog);
    rec.dialog = undefined;
  }
  rec.style?.remove();
  bump();
}

// ---------------------------------------------------------------------------
// Command routing
// ---------------------------------------------------------------------------

export type CommandRoute =
  | { kind: "handler" }
  | { kind: "dialog"; surfaceId: string; ref?: string }
  | { kind: "pane"; surfaceId: string; ref?: string }
  | { kind: "activate" }
  | { kind: "unknown" };

/** The pure routing decision behind runCommand, split out for tests:
 * a registered handler always wins (which is what makes commands.run safe
 * during the caller's own activate()); then a declarative manifest command
 * opens its surface without needing the module; then an idle/loading module is
 * activated and asked again; anything else is unknown. */
export function routeCommand(
  commandId: string,
  view: { registered: boolean; status: ExtStatus; spec: ExtensionSpec }
): CommandRoute {
  if (view.registered) return { kind: "handler" };
  const cmd = view.spec.commands.find((c) => c.id === commandId);
  if (cmd?.surface) {
    const surface = view.spec.surfaces.find((s) => s.id === cmd.surface);
    if (surface) {
      return surface.kind === "dialog"
        ? { kind: "dialog", surfaceId: surface.id, ref: cmd.ref || undefined }
        : { kind: "pane", surfaceId: surface.id, ref: cmd.ref || undefined };
    }
  }
  if (view.status === "idle" || view.status === "loading") return { kind: "activate" };
  return { kind: "unknown" };
}

/** Run one extension command (bar button, palette entry, extension code). */
export async function runCommand(extId: string, commandId: string, ...args: unknown[]): Promise<void> {
  const ext = known.get(extId);
  if (!ext) {
    extError(extId, "runCommand: unknown or disabled extension");
    return;
  }
  const rec = getRecord(ext);
  const invoke = (): boolean => {
    const handler = rec.commands.get(commandId);
    if (!handler) return false;
    try {
      handler(...args);
    } catch (err) {
      extError(extId, "command " + commandId + " failed", err);
      toast("Extension " + (ext.label || extId) + ": " + commandId + " failed");
    }
    return true;
  };
  const route = routeCommand(commandId, {
    registered: rec.commands.has(commandId),
    status: rec.status,
    spec: ext.extension,
  });
  switch (route.kind) {
    case "handler":
      invoke();
      return;
    case "dialog":
      // The surface body needs the module — openExtDialog kicks activation.
      openExtDialog(extId, route.surfaceId, route.ref);
      return;
    case "pane":
      openExtPane(extId, route.surfaceId, { ref: route.ref });
      return;
    case "activate":
      if (rec.activating) {
        // Awaiting our own activation would deadlock; register before running.
        extError(extId, "command " + commandId + " ran during activate() before being registered");
        return;
      }
      await activateExtension(ext);
      if (!invoke() && rec.status === "active") {
        extError(extId, "command " + commandId + " is not registered");
        toast("Extension " + (ext.label || extId) + ": unknown command " + commandId);
      }
      return;
    default:
      // Active (or failed) with neither a handler nor a declared surface.
      extError(extId, "command " + commandId + " is not registered");
      if (rec.status === "active") {
        toast("Extension " + (ext.label || extId) + ": unknown command " + commandId);
      }
  }
}

// ---------------------------------------------------------------------------
// Panes
// ---------------------------------------------------------------------------

function makeSurfaceEl(extId: string): HTMLElement {
  const el = document.createElement("div");
  // .mfx-<id> on the root gives the extension's namespaced selectors
  // (".mfx-<id> .foo") a guaranteed ancestor to hang from.
  el.className = "ext-surface mfx-" + extId;
  return el;
}

/** Open (or reveal) an extension pane. Returns the full pane key, or "" on a
 * contract violation (attributed error, no crash). */
export function openExtPane(
  extId: string,
  surfaceId: string,
  opts?: { ref?: string; title?: string; ctx?: unknown }
): string {
  const ext = known.get(extId);
  if (!ext) {
    extError(extId, "openPane: unknown or disabled extension");
    return "";
  }
  const rec = getRecord(ext);
  const surface = ext.extension.surfaces.find((s) => s.id === surfaceId && s.kind === "pane");
  if (!surface) {
    extError(extId, "openPane: no pane surface " + JSON.stringify(surfaceId));
    return "";
  }
  let ref = opts?.ref;
  if (surface.multi) {
    if (!ref) {
      // Host-minted opaque instance token — monotonic per surface, so two
      // "new query" clicks can never collide.
      const n = (rec.refCounters.get(surfaceId) || 0) + 1;
      rec.refCounters.set(surfaceId, n);
      ref = "#" + n;
    }
  } else if (ref) {
    extError(extId, "openPane: surface " + surfaceId + " is single-instance — ref not allowed");
    return "";
  }
  const key = buildTarget(extId, surfaceId, ref);
  const title = opts?.title || surface.title || ext.label;
  if (!rec.panes.has(key)) {
    rec.panes.set(key, {
      kind: "pane",
      el: makeSurfaceEl(extId),
      surfaceId,
      ref,
      ctx: opts?.ctx,
      started: false,
      epoch: ++epochCounter,
    });
  }
  // Same-key open = reveal + retitle (the store's openExtPane is idempotent
  // by key); the existing body is never re-rendered.
  useUi.getState().openExtPane(key, title);
  // The dialog→pane flow must not strand the pane behind a full-screen modal
  // (VerifyDialog precedent): opening a pane closes this extension's own
  // dialog, if that is what's on top.
  const ui = useUi.getState();
  if (
    ui.openDialog === "extension" &&
    ui.dialogTarget &&
    parseTarget(ui.dialogTarget).extId === extId
  ) {
    ui.closeDialog();
  }
  // The pane body needs the module — make sure activation has started.
  void activateExtension(ext);
  bump();
  return key;
}

/** Close a pane by surface + ref (the api.ui.closePane shape). */
export function closeExtPane(extId: string, surfaceId: string, ref?: string) {
  const ext = known.get(extId) || records.get(extId)?.ext;
  const surface = ext?.extension.surfaces.find((s) => s.id === surfaceId && s.kind === "pane");
  if (surface?.multi && !ref) {
    extError(extId, "closePane: surface " + surfaceId + " is multi-instance — a ref is required");
    return;
  }
  closeExtPaneByKey(buildTarget(extId, surfaceId, ref));
}

/** Close a pane by its full key (the Close button, the reap, deactivation). */
export function closeExtPaneByKey(key: string) {
  const { extId } = parseTarget(key);
  const rec = records.get(extId);
  const runtime = rec?.panes.get(key);
  if (rec && runtime) {
    rec.panes.delete(key);
    disposeRuntime(extId, runtime);
  }
  useUi.getState().closeExtPane(key);
  bump();
}

// ---------------------------------------------------------------------------
// Dialogs
// ---------------------------------------------------------------------------

/** Open an extension dialog surface (default: the first kind="dialog" one). */
export function openExtDialog(extId: string, surfaceId?: string, ref?: string, ctx?: unknown) {
  const ext = known.get(extId);
  if (!ext) {
    extError(extId, "openDialog: unknown or disabled extension");
    return;
  }
  const rec = getRecord(ext);
  const surface = surfaceId
    ? ext.extension.surfaces.find((s) => s.id === surfaceId && s.kind === "dialog")
    : ext.extension.surfaces.find((s) => s.kind === "dialog");
  if (!surface) {
    extError(extId, "openDialog: no dialog surface" + (surfaceId ? " " + JSON.stringify(surfaceId) : ""));
    return;
  }
  const target = buildTarget(extId, surface.id, ref);
  ensureDialogRuntime(rec, target, surface.id, ref, ctx);
  useUi.getState().openDialogFor("extension", target);
  void activateExtension(ext);
  bump();
}

function ensureDialogRuntime(
  rec: ExtRecord,
  target: string,
  surfaceId: string,
  ref?: string,
  ctx?: unknown
) {
  if (rec.dialog && rec.dialog.target !== target) {
    // Same extension, different surface/ref: the old body is done for —
    // dialog bodies are kept only for the SAME target while it stays open.
    disposeRuntime(rec.ext.id, rec.dialog);
    rec.dialog = undefined;
  }
  if (!rec.dialog) {
    const surface = rec.ext.extension.surfaces.find((s) => s.id === surfaceId);
    rec.dialog = {
      kind: "dialog",
      target,
      el: makeSurfaceEl(rec.ext.id),
      surfaceId,
      ref,
      ctx,
      started: false,
      epoch: ++epochCounter,
      title: surface?.title || rec.ext.label,
    };
  }
}

/** api.ui.closeDialog — GUARDED: a no-op unless the dialog on screen belongs
 * to the calling extension, so no extension can swat someone else's modal. */
export function closeExtDialog(extId: string) {
  const s = useUi.getState();
  if (s.openDialog !== "extension" || !s.dialogTarget) return;
  if (parseTarget(s.dialogTarget).extId !== extId) return;
  // The store close triggers ExtensionDialog's effect cleanup, which releases
  // (disposes) the body via releaseDialogTarget.
  s.closeDialog();
}

/** Dispose a dialog body once its target is no longer on screen — called from
 * ExtensionDialog's effect cleanup. Dialogs are transient: extensions needing
 * sticky dialog state keep it in module state, not in the DOM. */
export function releaseDialogTarget(target: string) {
  const { extId } = parseTarget(target);
  const rec = records.get(extId);
  if (rec?.dialog && rec.dialog.target === target) {
    disposeRuntime(extId, rec.dialog);
    rec.dialog = undefined;
    bump();
  }
}

// ---------------------------------------------------------------------------
// Mount / start / dispose (the keep-alive contract)
// ---------------------------------------------------------------------------

function liveRuntime(extId: string, key: string, kind: "pane" | "dialog"): SurfaceRuntime | undefined {
  const rec = records.get(extId);
  if (!rec) return undefined;
  if (kind === "pane") return rec.panes.get(key);
  return rec.dialog?.target === key ? rec.dialog : undefined;
}

/** Adopt a pane's keep-alive body into a React-owned container. Returns the
 * detach cleanup — detach only; dispose happens on close/deactivate. */
export function mountExtPane(key: string, host: HTMLElement): () => void {
  const { extId } = parseTarget(key);
  const rec = records.get(extId);
  const runtime = rec?.panes.get(key);
  if (!rec || !runtime) return () => {};
  host.appendChild(runtime.el);
  void startRuntime(rec, key, runtime);
  return () => {
    runtime.el.remove();
  };
}

/** Adopt a dialog body for a target, creating the runtime when the target
 * arrived through the store alone (a restored dialog slot). Same detach-only
 * cleanup contract as mountExtPane; ExtensionDialog pairs it with
 * releaseDialogTarget when the dialog actually closes. */
export function mountExtDialog(target: string, host: HTMLElement): () => void {
  const { extId, surfaceId, ref } = parseTarget(target);
  const ext = known.get(extId);
  if (!ext) return () => {};
  const rec = getRecord(ext);
  ensureDialogRuntime(rec, target, surfaceId, ref);
  const runtime = rec.dialog!;
  host.appendChild(runtime.el);
  void activateExtension(ext);
  void startRuntime(rec, target, runtime);
  return () => {
    runtime.el.remove();
  };
}

/** Run the surface renderer once per instance, at first mount. Everything
 * after the await re-checks the live registry: the instance (or the whole
 * extension) may have been closed while the module loaded, and a stale start
 * must dispose itself and render nothing. */
async function startRuntime(rec: ExtRecord, key: string, runtime: SurfaceRuntime) {
  if (runtime.started || runtime.disposed) return;
  const token = runtime.epoch;
  await activateExtension(rec.ext);
  const live = liveRuntime(rec.ext.id, key, runtime.kind);
  if (!live || live.epoch !== token) {
    disposeRuntime(rec.ext.id, runtime);
    return;
  }
  const liveRec = records.get(rec.ext.id);
  if (!liveRec || liveRec.status !== "active") {
    // The record-level activation error renders instead.
    bump();
    return;
  }
  if (runtime.started) return;
  const renderer = liveRec.surfaces.get(runtime.surfaceId);
  if (!renderer) {
    runtime.error = "surface " + JSON.stringify(runtime.surfaceId) + " was never registered";
    extError(rec.ext.id, runtime.error);
    bump();
    return;
  }
  runtime.started = true;
  try {
    runtime.cleanup = renderer(makeSurfaceHost(liveRec, key, runtime));
  } catch (err) {
    runtime.error = errText(err);
    extError(rec.ext.id, "surface " + runtime.surfaceId + " failed to render", err);
  }
  bump();
}

function makeSurfaceHost(rec: ExtRecord, key: string, runtime: SurfaceRuntime): SurfaceHost {
  return {
    el: runtime.el,
    surfaceId: runtime.surfaceId,
    ref: runtime.ref,
    ctx: runtime.ctx,
    setTitle(title: string) {
      if (runtime.kind === "pane") {
        useUi.getState().retitleExtPane(key, title);
      } else {
        runtime.title = title;
        bump();
      }
    },
    close() {
      if (runtime.kind === "pane") closeExtPaneByKey(key);
      else closeExtDialog(rec.ext.id);
    },
  };
}

function disposeRuntime(extId: string, runtime: SurfaceRuntime) {
  if (runtime.disposed) return;
  runtime.disposed = true;
  if (runtime.cleanup) {
    try {
      runtime.cleanup.dispose();
    } catch (err) {
      extError(extId, "surface " + runtime.surfaceId + " dispose failed", err);
    }
  }
  runtime.cleanup = undefined;
  runtime.el.remove();
}

// ---------------------------------------------------------------------------
// Registrations (called from extension code via the api object)
// ---------------------------------------------------------------------------

const NOOP_DISPOSABLE: Disposable = { dispose() {} };

function registerCommand(
  extId: string,
  commandId: string,
  handler: (...args: unknown[]) => void
): Disposable {
  const rec = records.get(extId);
  if (!rec) return NOOP_DISPOSABLE;
  if (!commandId.startsWith(extId + ".")) {
    extError(extId, "commands.register: " + commandId + " must carry the " + extId + ". prefix");
    return NOOP_DISPOSABLE;
  }
  rec.commands.set(commandId, handler);
  const d: Disposable = {
    dispose() {
      if (rec.commands.get(commandId) === handler) rec.commands.delete(commandId);
    },
  };
  rec.disposables.push(d);
  return d;
}

function registerSurface(extId: string, surfaceId: string, renderer: SurfaceRenderer): Disposable {
  const rec = records.get(extId);
  if (!rec) return NOOP_DISPOSABLE;
  rec.surfaces.set(surfaceId, renderer);
  const d: Disposable = {
    dispose() {
      if (rec.surfaces.get(surfaceId) === renderer) rec.surfaces.delete(surfaceId);
    },
  };
  rec.disposables.push(d);
  // An already-adopted instance that was waiting on this renderer (registered
  // later than usual) can start now.
  for (const [key, runtime] of rec.panes) {
    if (runtime.surfaceId === surfaceId && !runtime.started && runtime.el.isConnected) {
      runtime.error = undefined;
      void startRuntime(rec, key, runtime);
    }
  }
  if (
    rec.dialog &&
    rec.dialog.surfaceId === surfaceId &&
    !rec.dialog.started &&
    rec.dialog.el.isConnected
  ) {
    rec.dialog.error = undefined;
    void startRuntime(rec, rec.dialog.target, rec.dialog);
  }
  bump();
  return d;
}

// ---------------------------------------------------------------------------
// The API object
// ---------------------------------------------------------------------------

function makeStorage(extId: string): ExtensionApi["storage"] {
  const prefix = "mfx:" + extId + ":";
  return {
    get<T>(key: string, fallback: T): T {
      try {
        const raw = localStorage.getItem(prefix + key);
        return raw === null ? fallback : (JSON.parse(raw) as T);
      } catch {
        return fallback;
      }
    },
    set(key: string, value: unknown): void {
      try {
        localStorage.setItem(prefix + key, JSON.stringify(value));
      } catch {
        /* storage unavailable */
      }
    },
  };
}

function deepFreeze<T>(obj: T): T {
  if (obj && typeof obj === "object" && !Object.isFrozen(obj)) {
    Object.freeze(obj);
    for (const v of Object.values(obj as Record<string, unknown>)) deepFreeze(v);
  }
  return obj;
}

/** Build one extension's frozen api object. Sub-objects are fresh per
 * extension and frozen individually; the manifest is a deep-frozen
 * structuredClone. The shared live objects (window.mindflock.events, the
 * sessions getter) are handed over UNfrozen on purpose — they mutate
 * (lastSeq, connected) for the whole app, and freezing them here would break
 * the bus for everyone. */
function makeApi(ext: ExtensionInfo): ExtensionApi {
  const extId = ext.id;
  const manifest = deepFreeze(structuredClone(ext.extension));
  const apiObj: ExtensionApi = {
    id: extId,
    apiVersion: HOST_API_VERSION,
    manifest,
    ui: {
      registerSurface: (surfaceId, renderer) => registerSurface(extId, surfaceId, renderer),
      openDialog: (surfaceId?, ref?, ctx?) => openExtDialog(extId, surfaceId, ref, ctx),
      closeDialog: () => closeExtDialog(extId),
      openPane: (surfaceId, opts) => openExtPane(extId, surfaceId, opts),
      closePane: (surfaceId, ref) => closeExtPane(extId, surfaceId, ref),
      toast: (msg, opts) => toast(msg, { duration: opts?.duration }),
    },
    commands: {
      register: (commandId, handler) => registerCommand(extId, commandId, handler),
      run: (commandId, ...args) => {
        if (!commandId.startsWith(extId + ".")) {
          // v1 scoping: an extension runs its OWN commands only.
          extError(extId, "commands.run: " + commandId + " is not this extension's command");
          return Promise.resolve();
        }
        return runCommand(extId, commandId, ...args);
      },
    },
    request: (path: string, opts?: ApiOptions) => api(path, opts),
    storage: makeStorage(extId),
    log: { error: (msg, err) => extError(extId, msg, err) },
  };
  // Shared seams under the v1 trust model — feature-detected, never frozen.
  const mf = window.mindflock as
    | (NonNullable<Window["mindflock"]> & { sessions?: () => unknown[] })
    | undefined;
  if (mf?.events) apiObj.events = mf.events;
  if (typeof mf?.sessions === "function") apiObj.sessions = mf.sessions;
  Object.freeze(apiObj.ui);
  Object.freeze(apiObj.commands);
  Object.freeze(apiObj.storage);
  Object.freeze(apiObj.log);
  Object.freeze(apiObj);
  return apiObj;
}

// ---------------------------------------------------------------------------
// Read accessors for the React shells
// ---------------------------------------------------------------------------

export interface SurfaceView {
  status: "loading" | "ready" | "error";
  error?: string;
  /** For "Loading <label>…" and error attribution. */
  label: string;
  /** Dialog chrome title (surface default, setTitle override). */
  title?: string;
}

function surfaceView(extId: string, runtime: SurfaceRuntime | undefined): SurfaceView {
  const ext = known.get(extId);
  const label = ext?.label || extId;
  const rec = records.get(extId);
  if (!rec || !runtime || runtime.disposed) {
    return { status: "error", error: "this window's extension is gone", label };
  }
  if (rec.status === "error") {
    return { status: "error", error: rec.error, label, title: runtime.title };
  }
  if (runtime.error) return { status: "error", error: runtime.error, label, title: runtime.title };
  if (!runtime.started) return { status: "loading", label, title: runtime.title };
  return { status: "ready", label, title: runtime.title };
}

export function extPaneView(key: string): SurfaceView {
  const { extId } = parseTarget(key);
  return surfaceView(extId, records.get(extId)?.panes.get(key));
}

export function extDialogView(target: string): SurfaceView {
  const { extId } = parseTarget(target);
  const rec = records.get(extId);
  return surfaceView(extId, rec?.dialog?.target === target ? rec.dialog : undefined);
}

/** The Settings row's error line: why activation failed, if it did. */
export function extActivationError(extId: string): string | undefined {
  const rec = records.get(extId);
  return rec?.status === "error" ? rec.error || "activation failed" : undefined;
}
