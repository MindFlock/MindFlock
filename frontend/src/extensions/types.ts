/** Extension system types (Addon API v3) — the frontend mirror of the backend's
 * ExtensionSpec dataclasses (backend/web/addons/base.py) plus the ExtensionApi
 * contract handed to each extension's activate(). The shapes here are the
 * contract the dbclient extension (and every future one) codes against, so
 * names are frozen once shipped — additions bump HOST_API_VERSION in host.ts,
 * renames never happen. */

/** One bar button: host-rendered chrome that runs a command on click. */
export interface ExtensionButton {
  command: string;
  label: string;
  /** Tooltip. */
  title?: string;
}

/** One command ("<ext-id>.<verb>"). `surface` makes it declarative: the host
 * can open that surface without loading the extension's module. */
export interface ExtensionCommand {
  id: string;
  /** Palette text, "Database: Explorer" style. */
  title: string;
  surface?: string | null;
  /** Instance ref for a declarative open (only meaningful with `surface`). */
  ref?: string | null;
}

/** A declared dialog (popup) or pane (grid window) whose body the extension
 * renders into a host-owned keep-alive container. */
export interface ExtensionSurface {
  id: string;
  kind: "dialog" | "pane";
  /** Default chrome title (setTitle can override per instance). */
  title: string;
  /** Pane only: many instances at once (the host mints refs). */
  multi?: boolean;
  /** Pane only: the host renders a back button in the pane head running this
   * command (the verify-pane-back precedent). */
  back_command?: string | null;
}

/** The static manifest served inside GET /api/addons under `extension`. */
export interface ExtensionSpec {
  /** URL of the ES module, e.g. "/extensions/dbclient/index.js". */
  module: string;
  bar_label: string;
  buttons: ExtensionButton[];
  commands: ExtensionCommand[];
  surfaces: ExtensionSurface[];
  /** Host injects <module dir>/style.css into layer(components). */
  stylesheet: boolean;
  /** MINIMUM host API level required. */
  api_version: number;
}

/** What useExtensions() exposes per addon carrying a non-null `extension`. */
export interface ExtensionInfo {
  id: string;
  label: string;
  enabled: boolean;
  /** Where the code lives — decides the Settings row's origin text. */
  origin?: "builtin" | "user";
  extension: ExtensionSpec;
}

/** Every registration returns one of these; the host drains them all when the
 * extension is disabled or errors. */
export interface Disposable {
  dispose(): void;
}

/** Options for api.request — the same shape the app's own api() wrapper takes. */
export type ApiOptions = RequestInit & { json?: unknown };

/** The event-bus seam (window.mindflock.events, provided by core/events.js) —
 * shared with the rest of the app, so the host hands it over unfrozen. */
export interface ExtensionEventEnvelope {
  seq: number;
  event: string;
  session: string;
  old: string | null;
  new: string | null;
  ts: number;
  data: Record<string, unknown>;
}

export interface MindflockEvents {
  subscribe(name: string, cb: (env: ExtensionEventEnvelope) => void): () => void;
  onStatus(cb: (s: "connected" | "disconnected") => void): () => void;
  isReplay(env: ExtensionEventEnvelope): boolean;
  connected: boolean;
  lastSeq: number;
}

/** What a surface renderer receives: the host-owned keep-alive container plus
 * the instance's identity and per-open context. */
export interface SurfaceHost {
  /** Host-owned container (keep-alive; style yourself inside it). */
  el: HTMLElement;
  surfaceId: string;
  ref?: string;
  /** The opts.ctx passed at open (in-memory only). */
  ctx?: unknown;
  setTitle(title: string): void;
  close(): void;
}

export type SurfaceRenderer = (host: SurfaceHost) => Disposable | void;

/** The one deep-frozen object activate(api) receives (host API level 1). */
export interface ExtensionApi {
  readonly id: string;
  /** The HOST's level. */
  readonly apiVersion: number;
  /** structuredClone, frozen. */
  readonly manifest: ExtensionSpec;
  ui: {
    registerSurface(surfaceId: string, renderer: SurfaceRenderer): Disposable;
    openDialog(surfaceId?: string, ref?: string, ctx?: unknown): void;
    /** GUARDED: no-op unless the open dialog is this extension's. */
    closeDialog(): void;
    /** Returns the pane key. */
    openPane(surfaceId: string, opts?: { ref?: string; title?: string; ctx?: unknown }): string;
    closePane(surfaceId: string, ref?: string): void;
    toast(msg: string, opts?: { duration?: number }): void;
  };
  commands: {
    /** Own prefix enforced. */
    register(commandId: string, handler: (...args: unknown[]) => void): Disposable;
    /** Own commands only (v1). */
    run(commandId: string, ...args: unknown[]): Promise<void>;
  };
  /** window.mindflock.events — shared, feature-detect. */
  events?: MindflockEvents;
  /** Shared under the v1 trust model (documented). */
  sessions?: () => unknown[];
  /** The app's api() wrapper. */
  request(path: string, opts?: ApiOptions): Promise<unknown>;
  /** localStorage "mfx:<id>:<key>", try/catch. */
  storage: { get<T>(key: string, fallback: T): T; set(key: string, value: unknown): void };
  log: { error(msg: string, err?: unknown): void };
}

/** What an extension module exposes: a default export (or a registration on
 * window.mindflockExtensions[id]) carrying activate(). */
export interface ExtensionModule {
  activate(api: ExtensionApi): void | Promise<void>;
}
