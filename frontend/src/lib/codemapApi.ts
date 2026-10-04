/** Code map fetchers — every route the Map tab and the Zones dialog call, in
 * one module, each with a normalizer.
 *
 * WHY one module: the file-card / search routes (backend/web/core/code_outline.py)
 * and the v3 zone routes were built alongside this client. Keeping every URL and every
 * "what if the server sent it the other way" rule here means a shape change is
 * one edit, and the components only ever see the normalized types in
 * api/types.ts. Normalizers never throw: a missing array becomes [], a
 * missing number 0, so a half-built response renders as "less", not a crash. */

import { api, instApi } from "../api/client";
import type {
  CodeMapLive,
  CodeMapSnapshot,
  EntryPoint,
  FileView,
  OutlineSymbol,
  RedZone,
  RedZonePreview,
  SearchItem,
} from "../api/types";

type Obj = Record<string, unknown>;
const arr = <T = unknown>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
const num = (v: unknown, d = 0): number => (typeof v === "number" && isFinite(v) ? v : d);
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});

const enc = encodeURIComponent;

// --- Normalizers -------------------------------------------------------------

function normSym(v: unknown): OutlineSymbol {
  const o = obj(v);
  return {
    name: str(o.name),
    kind: str(o.kind, "function"),
    line: num(o.line),
    end: num(o.end),
    public: o.public !== false,
    sig: str(o.sig),
    parent: o.parent == null ? null : str(o.parent),
    children: arr(o.children).map(normSym),
  };
}

function normEntry(v: unknown): EntryPoint {
  const o = obj(v);
  return {
    kind: str(o.kind, "http"),
    method: str(o.method, "ANY"),
    route: str(o.route),
    line: num(o.line),
    handler: str(o.handler),
    path: o.path == null ? undefined : str(o.path),
    changed: o.changed === true ? true : undefined,
  };
}

const normDep = (v: unknown) => ({ path: str(obj(v).path), names: arr(obj(v).names).map((x) => str(x)) });

export function normFileView(v: unknown): FileView {
  const o = obj(v);
  const imp = obj(o.imports);
  const z = obj(o.zones);
  return {
    path: str(o.path),
    lang: str(o.lang),
    loc: num(o.loc),
    symbols: arr(o.symbols).map(normSym),
    imports: { internal: arr(imp.internal).map(normDep).filter((d) => d.path), external: arr(imp.external).map((x) => str(x)) },
    entry: arr(o.entry).map(normEntry),
    used_by: arr(o.used_by).map(normDep).filter((d) => d.path),
    changed_lines: arr(o.changed_lines)
      .filter((h) => Array.isArray(h) && h.length >= 2)
      .map((h) => [num((h as unknown[])[0]), num((h as unknown[])[1])] as [number, number]),
    changed_symbols: arr(o.changed_symbols).map((x) => str(x)),
    zones: { red: !!z.red, green: z.green == null ? null : !!z.green },
    partial: !!o.partial,
    tested_by: arr(o.tested_by).map((x) => str(x)),
  };
}

// --- Tree cards + search -------------------------------------------------------

/** GET /code-map/file?path= — imports / outline / entry points / used-by. */
export async function fetchFileView(title: string, path: string): Promise<FileView> {
  return normFileView(await instApi<unknown>(title, "/code-map/file?path=" + enc(path)));
}

/** GET /code-map/search?q= — files, folders and symbols. */
export async function searchCode(title: string, q: string): Promise<SearchItem[]> {
  const d = obj(await instApi<unknown>(title, "/code-map/search?q=" + enc(q)));
  return arr(d.items).map((v) => {
    const o = obj(v);
    return { path: str(o.path), name: str(o.name), kind: str(o.kind, "file"), line: num(o.line), score: num(o.score) };
  });
}

// --- v2 snapshot + live ----------------------------------------------------------

/** GET /code-map (files + import edges, for blast and overlays). */
export function fetchSnapshot(title: string, fp?: string | null) {
  return instApi<CodeMapSnapshot | { unchanged: true; fingerprint: string }>(
    title,
    "/code-map" + (fp ? "?fp=" + enc(fp) : "")
  );
}

/** GET /code-map/live?since= — the poll. */
export function fetchLive(title: string, since: number) {
  return instApi<CodeMapLive>(title, "/code-map/live?since=" + since);
}

// --- Plan loop --------------------------------------------------------------------

export type Told = { ok?: boolean; told: string | false; reason?: string; zones?: RedZone[] };

export function askPlan(title: string, mode: "plan" | "remaining") {
  return instApi<Told>(title, "/code-map/ask-plan", { json: { mode } });
}

/** The Go answer. With `scope_to_plan`, `zones` is only the zones it CREATED
 * and `exempt` the already-changed files now outside the scope (exempt by
 * default — the Plan panel offers "Treat as breaches"). */
export type GoResult = Told & { exempt?: string[] };

/** POST /code-map/go. `scope_to_plan` = "Go — only the planned files": the
 * server turns the plan's paths into anchored worktree green zones first. */
export function goPlan(title: string, zoneIds: string[], scopeToPlan: boolean) {
  return instApi<GoResult>(title, "/code-map/go", { json: { zone_ids: zoneIds, scope_to_plan: scopeToPlan } });
}

// --- Zones (per session) ------------------------------------------------------------

export interface AddZoneBody {
  pattern: string;
  name: string;
  note: string;
  scope: "repo" | "worktree";
  tell_agent: boolean;
  kind: "red" | "green";
}

export interface AddZoneResult extends Told {
  zone: RedZone;
  zones: RedZone[];
  already_changed?: string[];
  /** Green: paths already changed outside the new scope, exempted. */
  exempt?: string[];
  committed_outside?: number;
}

export function addZone(title: string, body: AddZoneBody) {
  return instApi<AddZoneResult>(title, "/red-zones", { json: body });
}

export function previewZone(title: string, pattern: string, kind: "red" | "green") {
  return instApi<RedZonePreview>(title, "/red-zones/preview", { json: { pattern, kind } });
}

export function removeZone(title: string, id: string) {
  return instApi<{ ok: boolean; zones: RedZone[] }>(title, "/red-zones/" + enc(id), { method: "DELETE" });
}

export function waiveZone(title: string, id: string, waived: boolean) {
  return instApi<{ ok: boolean; zones: RedZone[] }>(title, "/red-zones/" + enc(id) + "/waive", { json: { waived } });
}

/** POST /red-zones/allow {path} — "Allow this file": an anchored worktree
 * green zone for exactly that path; the server tells the agent. */
export function allowPath(title: string, path: string) {
  return instApi<AddZoneResult>(title, "/red-zones/allow", { json: { path } });
}

/** POST /red-zones/exempt {paths, exempt:false} — "Treat as breaches": drop
 * the exemption a green add gave the already-changed files. */
export function setExempt(title: string, paths: string[], exempt: boolean) {
  return instApi<{ ok: boolean; zones?: RedZone[] }>(title, "/red-zones/exempt", { json: { paths, exempt } });
}

// --- Repo-level (Zones dialog) ---------------------------------------------------------

export interface CompanionsDoc {
  /** The user's configured derived outputs for the repo. */
  companions: string[];
  /** The built-in set (lockfiles, snapshots, tests importing the zone) —
   * shown, not editable. */
  defaults: string[];
}

/** `{repo_id, patterns, defaults}` — the server calls the list `patterns`. */
function normCompanions(v: unknown): CompanionsDoc {
  const d = obj(v);
  const list = Array.isArray(d.patterns) ? d.patterns : d.companions;
  return { companions: arr(list).map((x) => str(x)), defaults: arr(d.defaults).map((x) => str(x)) };
}

export async function fetchCompanions(repoId: string): Promise<CompanionsDoc> {
  return normCompanions(await api<unknown>("/api/red-zones/companions?repo_id=" + enc(repoId)));
}

/** `label` names the repo in the store when this is its first entry (the
 * server otherwise labels it with its raw id, as for plan-first). */
export async function saveCompanions(repoId: string, companions: string[], label = ""): Promise<CompanionsDoc> {
  return normCompanions(
    await api<unknown>("/api/red-zones/companions", {
      method: "PUT",
      json: { repo_id: repoId, patterns: companions, ...(label ? { label } : {}) },
    })
  );
}
