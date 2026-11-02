/** The Sky's new-session block, the pure half (docs/flights.md "As built:
 * round 12 new block"): the More options fold's choices and their defaults,
 * the one-line summary the closed fold shows, and From a session — which
 * sessions can be forked from, the worker-style title, the lineage keys.
 *
 * Everything here feeds the New dialog's own body builder
 * (`newSessionSubmit(ctx).buildBody`): these are the ctx fields the block
 * fills, never a second body shape. */

import { tokenize } from "../components/dialogs/FlagChips";

/** localStorage: whether More options is open ("1") or folded. */
export const MORE_KEY = "mf_sky_new_more";

/** Past this many sessions the From a session picker gets a filter field. */
export const PARENT_FILTER_AT = 8;

/** What More options decides — the New dialog's fields of the same names. */
export interface BlockOptions {
  /** "" = the app's default account; "default" = the CLI's own login. */
  profileId: string;
  /** A key account's model ("" = the account's own pin). */
  profileModel: string;
  /** Work in the folder itself instead of a worktree of its own. */
  inPlace: boolean;
  /** The worktree's base ("" = the server's choice). */
  baseBranch: string;
  /** The base is a remote-tracking name ("origin/feature"). */
  baseRemote: boolean;
  /** The template picked ("" = none). */
  template: string;
  /** A template's workspace that the block carries but doesn't offer. */
  provision: boolean;
  strategy: string;
  initRepo: boolean;
  /** "" = this device; else a connected tailnet device. */
  device: string;
}

export function defaultOptions(): BlockOptions {
  return {
    profileId: "",
    profileModel: "",
    inPlace: false,
    baseBranch: "",
    baseRemote: false,
    template: "",
    provision: false,
    strategy: "worktree",
    initRepo: false,
    device: "",
  };
}

/** Whether More options is open (it starts folded). */
export function readMoreOpen(kv: Pick<Storage, "getItem"> | null = storage()): boolean {
  try {
    return kv?.getItem(MORE_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeMoreOpen(open: boolean, kv: Pick<Storage, "setItem"> | null = storage()): void {
  try {
    kv?.setItem(MORE_KEY, open ? "1" : "0");
  } catch {
    /* a private window: it just opens folded next time */
  }
}

function storage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** The model the session runs: a key account's pick, else a `--model` (or
 * `-m`, `--model=`) in its launch flags; "" = the CLI's own default. */
export function modelOf(profileModel: string, launchArgs: string): string {
  if (profileModel.trim()) return profileModel.trim();
  const toks = tokenize(launchArgs);
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i];
    if (t.startsWith("--model=")) return t.slice(8);
    if ((t === "--model" || t === "-m") && toks[i + 1] && !toks[i + 1].startsWith("-")) return toks[i + 1];
  }
  return "";
}

/** Where the session works, in words: "worktree from main", "in place",
 * "provisioned", or "worktree" when the server picks the base. */
export function workspaceWords(o: Pick<BlockOptions, "inPlace" | "baseBranch" | "provision">): string {
  if (o.provision) return "provisioned";
  if (o.inPlace) return "in place";
  return o.baseBranch.trim() ? "worktree from " + o.baseBranch.trim() : "worktree";
}

/** The closed fold's line: "claude · opus · worktree from main", then the
 * template and the device when they are picked. */
export function optionsSummary(p: {
  program: string;
  options: BlockOptions;
  launchArgs: string;
  /** The device's name in the picker ("" = this device). */
  deviceName?: string;
}): string {
  const o = p.options;
  const parts = [p.program.trim(), modelOf(o.profileModel, p.launchArgs), workspaceWords(o)];
  if (o.template) parts.push("“" + o.template + "”");
  if (o.device) parts.push("on " + (p.deviceName || o.device));
  return parts.filter(Boolean).join(" · ");
}

/** The base branch chips: the default first, then the checked-out one, then
 * the rest (local before remote), at most `max`. */
export function branchChoices(
  b: { default: string; current: string; branches: ReadonlyArray<{ name: string; remote: boolean }> } | null,
  max = 6
): Array<{ value: string; label: string; remote: boolean }> {
  if (!b) return [];
  const rank = (n: { name: string; remote: boolean }) => (n.name === b.default ? 0 : n.name === b.current ? 1 : n.remote ? 3 : 2);
  const sorted = [...b.branches].sort((x, y) => rank(x) - rank(y));
  const out: Array<{ value: string; label: string; remote: boolean }> = [];
  for (const n of sorted) {
    if (!n.name || out.some((o) => o.value === n.name)) continue;
    out.push({ value: n.name, label: n.name, remote: n.remote });
    if (out.length >= max) break;
  }
  return out;
}

/* ---- From a session ---- */

/** A session row, as much of it as forking needs. */
export interface ParentRow {
  title: string;
  path: string;
  branch: string;
  program?: string;
  provider?: string;
  device?: string;
  hidden_by?: string;
  locked?: boolean;
  pending_create?: boolean;
}

/** A session the block can fork from: its title, its name, its repository. */
export interface ParentChoice {
  title: string;
  name: string;
  repo: string;
  branch: string;
  provider: string;
}

/** The live sessions on THIS device worth forking from — not another
 * device's (the fork reads its repository here), not one still being made,
 * not a feature's hidden session — by name. */
export function parentChoices(rows: readonly ParentRow[], nameOf: (title: string) => string = (t) => t): ParentChoice[] {
  return rows
    .filter((r) => !r.device && !r.hidden_by && !r.locked && !r.pending_create && !!r.path && !r.title.includes("::"))
    .map((r) => ({
      title: r.title,
      name: nameOf(r.title) || r.title,
      repo: r.path,
      branch: r.branch || "",
      provider: r.provider || r.program || "",
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** The picker's filter: every word in the name, title or repository. */
export function filterParents(choices: readonly ParentChoice[], q: string): ParentChoice[] {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [...choices];
  return choices.filter((c) => {
    const hay = (c.name + " " + c.title + " " + c.repo).toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** The MCP's slug (backend/mcp/tools.py `_slug`). */
function slug(text: string): string {
  return (text || "").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "");
}

/** A worker's title the way `spawn_session` names one: the lowest free
 * `<parent>-w<N>` (the parent slugged, at most 40 characters). */
export function workerTitle(parent: string, taken: Iterable<string>): string {
  const s = slug(parent).slice(0, 40);
  const prefix = s ? s + "-w" : "worker-";
  const used = new Set(taken);
  let n = 1;
  while (used.has(prefix + n)) n++;
  return prefix + n;
}

/** What a fork does to the block's options: a worktree (never in place,
 * never provisioned) on this device, based on the parent's branch — so the
 * body builder cuts it at the parent's HEAD and reads its diff against it, as
 * `spawn_session` does. */
export function forkOptions(o: BlockOptions, parent: Pick<ParentChoice, "branch">): BlockOptions {
  return { ...o, inPlace: false, provision: false, strategy: "worktree", initRepo: false, device: "", baseBranch: parent.branch, baseRemote: false };
}

/** The lineage keys a fork adds to the create body (the server's
 * `parent` / `spawned`, as `spawn_session` sends them). */
export function forkFields(parent: Pick<ParentChoice, "title">): { parent: string; spawned: true } {
  return { parent: parent.title, spawned: true };
}
