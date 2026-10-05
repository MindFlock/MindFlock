/** MindFlock MCP from the UI: the named prompts ("playbooks") a session's agent
 * can be handed — Split across workers, Ask a session, Check on workers, Wrap
 * up workers — and every way in to them (the pane's fork-icon menu, the row ›
 * menu, the palette, Ctrl+K F, the New dialog's "Split across workers").
 *
 * A playbook is a PASTE, never a send: the server renders the prompt for this
 * session (`POST /api/playbooks/{id}/render`) and `/send {submit: false}` types
 * it into the agent's input box. Nothing runs until the user presses Enter, and
 * a playbook whose task is still missing ends on "The task: " so the caret is
 * already where it goes. The agent's own MindFlock tools then do the spawning,
 * waiting and merging, which is what keeps report-back, the safe-delete checks
 * and the scope limits in force.
 *
 * The pure half (gating, the menu's shape, the Ask list, the split heuristic)
 * is unit-tested in node; the rest is a thin layer over the API. */

import { api } from "../api/client";
import { childrenOf } from "./agentMessages";
import type { Caps, Config, Instance, Playbook, PlaybooksResponse } from "../api/types";
import { queryClient } from "../state/queries";
import { displayName, useUi } from "../state/store";
import { errMsg } from "./format";
import { instances, selectSession } from "./sessionActions";
import { focusTerm } from "./terminals";
import { toast } from "./toast";

// --- Why a session can't take one ------------------------------------------

/** The server's wording for the same three conditions (GET /api/playbooks'
 * `disabled_reason`), repeated here so the fork button can say it before the
 * menu has fetched anything. */
export const NO_TOOLS_REASON = "This CLI doesn't get the MindFlock tools";
export const RESTART_REASON = "Restart this agent to give it the MindFlock tools";
export const ANSWER_FIRST_REASON =
  "Answer its prompt first — pasting now would answer the dialog";

/** Does this session's CLI get the MindFlock tools at all? Read from
 * `caps.agent_mcp` (attach switched on, and this provider is one it attaches
 * to). An older server that doesn't report the cap gets no button: a menu of
 * prompts naming tools the agent may not have is worse than no menu. */
export function mcpCapable(
  caps: Partial<Caps> | undefined,
  inst: Pick<Instance, "provider" | "program"> & Partial<Pick<Instance, "title" | "device">>
): boolean {
  // Another device's session: the caps are THIS server's, and only
  // /api/instances/<dev::…> is forwarded — /api/playbooks would 404.
  if (isRemote(inst)) return false;
  const m = caps?.agent_mcp;
  if (!m || !m.enabled) return false;
  const provider = inst.provider || inst.program || "";
  return !!provider && (m.providers || []).includes(provider);
}

/** A row that belongs to another device (its title is `<device>::<title>`). */
export function isRemote(inst: Partial<Pick<Instance, "title" | "device">>): boolean {
  return !!inst.device || String(inst.title || "").includes("::");
}

/** Why the fork button is disabled right now, or "" when it isn't. Two cases,
 * both about THIS launch rather than the CLI: an agent started without the
 * attach args (a resume after a server restart) has no tools to follow the
 * prompt with, and an agent sitting in a permission dialog would take the
 * paste as its answer. `mcp_attached: null` (unknown) does not block. */
export function forkBlockReason(inst: Pick<Instance, "mcp_attached" | "activity">): string {
  if (inst.mcp_attached === false) return RESTART_REASON;
  if (inst.activity === "clarify" || inst.activity === "limit") return ANSWER_FIRST_REASON;
  return "";
}

/** The New dialog's "Split across workers" gate for the agent in its Agent
 * field. The capability being ABSENT (an older server, or config not loaded
 * yet) is "unknown", not "off": the box stays usable and the server's own 400
 * is the backstop. Present, it must be switched on and name this CLI. */
export function splitGate(
  caps: Partial<Caps> | undefined,
  provider: string
): { ok: boolean; reason: string } {
  const m = caps?.agent_mcp;
  if (!m) return { ok: true, reason: "" };
  if (!m.enabled)
    return {
      ok: false,
      reason: "MindFlock tools are switched off for new sessions — Settings → General",
    };
  if (!(m.providers || []).includes(provider)) {
    const names = (m.providers || []).map((p) => p.charAt(0).toUpperCase() + p.slice(1));
    return {
      ok: false,
      reason: names.length
        ? `Needs a CLI that gets the MindFlock tools — ${names.join(" or ")}`
        : NO_TOOLS_REASON,
    };
  }
  return { ok: true, reason: "" };
}

// --- The family -------------------------------------------------------------

/** A session's live workers — the one shared rule (agentMessages.isChildOf:
 * local, not pending, parent is it), so this menu's "workers · N", the
 * palette and the rail's roll-up always agree with the server. */
export function liveChildren(title: string, rows: readonly Instance[]): Instance[] {
  return childrenOf(title, rows);
}

/** "n of N reported" for Wrap up workers — a worker has reported once it has
 * a `last_report` to its current parent. */
export function reportedLabel(children: readonly Instance[]): string {
  const n = children.filter((c) => !!c.last_report).length;
  return `${n} of ${children.length} reported`;
}

/** Playbooks that only make sense with workers (the server omits them when
 * there are none). `when` is read when the server sends it; the ids are the
 * fallback for one that doesn't. */
const WORKER_PLAYBOOKS: ReadonlySet<string> = new Set(["workers", "wrapup"]);
export function isWorkerPlaybook(p: Pick<Playbook, "id" | "when">): boolean {
  return p.when === "has_children" || WORKER_PLAYBOOKS.has(p.id);
}

/** The fork-icon menu's shape: the general playbooks first, then — only when
 * the session has workers — a "<name>'s workers · N" section with the worker
 * playbooks. "Message…" is not a playbook (it opens the Thread composer as
 * the user) and the component appends it last. */
export interface PlaybookMenuModel {
  general: Playbook[];
  workers: { count: number; reported: string; items: Playbook[] } | null;
}

export function menuModel(
  playbooks: readonly Playbook[],
  children: readonly Instance[]
): PlaybookMenuModel {
  const general = playbooks.filter((p) => !isWorkerPlaybook(p));
  const items = playbooks.filter((p) => isWorkerPlaybook(p));
  const workers =
    children.length || items.length
      ? { count: children.length, reported: reportedLabel(children), items }
      : null;
  return { general, workers };
}

/** The menu's letter for a playbook (the registry's, upper-cased). */
export function letterOf(p: Pick<Playbook, "letter">): string {
  return String(p.letter || "").slice(0, 1).toUpperCase();
}

/** One row of the Ask › submenu. `slot` is the session's rail number ("" past
 * nine or off the rail), `rel` how it relates to the asker. */
export interface AskTarget {
  title: string;
  name: string;
  slot: string;
  rel: "worker" | "parent" | "sibling" | "";
  activity: string;
}

/** Who "Ask a session…" can ask: every other local session, in rail order
 * (the order the numbers on the rail are in), with the asker's own family
 * first — those are the ones an orchestrator or a worker asks. Remote rows
 * (another device's) and pending ones are out: the MCP's send_message only
 * reaches local, existing sessions. */
export function askTargets(
  self: string,
  rows: readonly Instance[],
  railOrder: readonly string[],
  nameOf: (t: string) => string
): AskTarget[] {
  const me = rows.find((r) => r.title === self);
  const candidates = rows.filter((r) => r.title !== self && !r.device && !r.pending);
  const rank = (t: string) => {
    const i = railOrder.indexOf(t);
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
  };
  const relOf = (r: Instance): AskTarget["rel"] => {
    if (r.parent === self) return "worker";
    if (me?.parent && r.title === me.parent) return "parent";
    if (me?.parent && r.parent === me.parent) return "sibling";
    return "";
  };
  const famRank = (rel: AskTarget["rel"]) =>
    rel === "parent" ? 0 : rel === "worker" ? 1 : rel === "sibling" ? 2 : 3;
  return candidates
    .map((r) => {
      const i = rank(r.title);
      return {
        title: r.title,
        name: nameOf(r.title),
        slot: i < 9 ? String(i + 1) : "",
        rel: relOf(r),
        activity: String(r.activity || ""),
        _i: i,
      };
    })
    .sort((a, b) => famRank(a.rel) - famRank(b.rel) || a._i - b._i)
    .map(({ _i: _unused, ...t }) => t);
}

// --- The heuristic behind the New dialog's "suggested" pill ------------------

/** A list of at least three short pieces joined the way a person lists them:
 * "billing, search and upload". The words on each side of the separators are
 * the pieces; an article after "and" is skipped. */
const LIST_RE =
  /\b([a-z][\w-]*)((?:\s*,\s*(?:the\s+)?[a-z][\w-]*)+),?\s+(?:and|&)\s+(?:the\s+|an?\s+)?([a-z][\w-]*)/i;
/** Saying it outright. */
const CUE_RE =
  /\b(?:in parallel|split (?:it |this |the work )?(?:across|into|between)|fan(?:ning)? (?:it )?out|one (?:worker|session|agent) (?:per|for each)|across (?:\w+ )?workers)\b/i;
const NOT_A_PIECE: ReadonlySet<string> = new Set(["it", "them", "then", "also", "this", "that"]);

/** Does the sentence describe several independent pieces? Returns the pieces
 * it named (possibly none, when it only said "in parallel"), or null. It only
 * ever SUGGESTS — the box is never ticked for the user. */
export function splitSuggestion(text: string): { pieces: string[] } | null {
  const s = String(text || "");
  const m = LIST_RE.exec(s);
  if (m) {
    const middle = m[2]
      .split(",")
      .map((x) => x.trim().replace(/^the\s+/i, ""))
      .filter(Boolean);
    const all = [m[1], ...middle, m[3]].map((x) => x.toLowerCase());
    const pieces = all.filter((x, i) => !NOT_A_PIECE.has(x) && all.indexOf(x) === i);
    if (pieces.length >= 3) return { pieces: pieces.slice(0, 5) };
  }
  if (CUE_RE.test(s)) return { pieces: [] };
  return null;
}

/** The pill's text: "suggested · billing · search · upload". */
export function suggestionPill(sug: { pieces: string[] }): string {
  return ["suggested", ...sug.pieces].join(" · ");
}

/** A create body with "Split across workers" applied. The server adds the
 * playbook to the prompt. Workers fork from a branch, so the session runs in
 * a worktree: an `in_place` the form was about to send becomes false. The
 * server forces that too; sending it here keeps the request matching what
 * the form showed. A provisioned body carries no `in_place` and keeps it
 * that way. */
export function withSplit(body: Record<string, unknown>, on: boolean): Record<string, unknown> {
  if (!on) return body;
  const next: Record<string, unknown> = { ...body, playbook: "split" };
  if ("in_place" in next) next.in_place = false;
  return next;
}

// --- Talking to the server --------------------------------------------------

/** The playbooks for one session (availability judged by the server), or the
 * bare registry when no title is given. */
export async function fetchPlaybooks(title?: string): Promise<Playbook[]> {
  const q = title ? "?title=" + encodeURIComponent(title) : "";
  const r = await api<PlaybooksResponse>("/api/playbooks" + q);
  return r?.playbooks || [];
}

/** Render a playbook for `title` and type it into its agent — never submitted.
 * Brings the pane forward on its Agent tab with the caret in the terminal, so
 * the next keystrokes add the task and Enter runs it. */
/** Sessions with a paste in flight: every surface (the fork menu, the row ›
 * menu, the palette, the rail chip, the Thread's buttons) goes through
 * here, so a double-click anywhere types the prompt once. */
const pasting = new Set<string>();

export async function pastePlaybook(
  title: string,
  pb: Pick<Playbook, "id" | "label">,
  args: Record<string, string> = {}
): Promise<boolean> {
  if (pasting.has(title)) return false;
  pasting.add(title);
  try {
    return await pasteNow(title, pb, args);
  } finally {
    pasting.delete(title);
  }
}

/** The paste itself. Both requests re-check the agent on the server, where
 * its state is current — the menu this was picked from can be a poll or two
 * behind, and pasted text holds digits ("api-billing-2") that would pick a
 * permission dialog's option: the render is 409 while it is on a prompt (or
 * this launch has no tools), and `dialog_safe` refuses to type into a dialog
 * that came up in between. */
async function pasteNow(
  title: string,
  pb: Pick<Playbook, "id" | "label">,
  args: Record<string, string>
): Promise<boolean> {
  const name = displayName(title);
  try {
    const { text } = await api<{ text: string }>(
      "/api/playbooks/" + encodeURIComponent(pb.id) + "/render",
      { json: { title, args } }
    );
    if (!text) throw new Error("the server rendered an empty prompt");
    await api("/api/instances/" + encodeURIComponent(title) + "/send", {
      json: { text, submit: false, dialog_safe: true },
    });
    // On the Agent tab — a paste into a pane showing its Diff is a paste
    // nobody sees — and with the keyboard in the terminal.
    const ui = useUi.getState();
    if (ui.lastTab[title] !== "agent") ui.setLastTab(title, "agent");
    selectSession(title);
    setTimeout(() => focusTerm(title), 60);
    const label = pb.label.replace(/…$/, "");
    toast(
      /:\s*$/.test(text)
        ? `Typed “${label}” into ${name} — add the task, then press Enter`
        : `Typed “${label}” into ${name} — press Enter to run it`,
      { duration: 4000 }
    );
    return true;
  } catch (err) {
    toast(`Couldn't type “${pb.label}” into ${name}: ${errMsg(err)}`, { duration: 6000 });
    return false;
  }
}

/** Run a playbook by id from a surface that has no list of its own yet (the
 * palette, a chip): ask the server whether it applies first, so a refusal is
 * the server's sentence rather than a failed render. */
export async function runPlaybook(
  title: string,
  id: string,
  args: Record<string, string> = {},
  /** What the toast calls it, when not the playbook's own label (the
   * Thread's "Merge api-billing into api" is the wrapup playbook scoped to
   * one worker). */
  label?: string
): Promise<boolean> {
  let list: Playbook[];
  try {
    list = await fetchPlaybooks(title);
  } catch (err) {
    toast("Couldn't load the playbooks: " + errMsg(err), { duration: 6000 });
    return false;
  }
  const pb = list.find((p) => p.id === id);
  if (!pb) {
    toast(`That isn't available for ${displayName(title)} right now`);
    return false;
  }
  if (!pb.available) {
    toast(pb.disabled_reason || `That isn't available for ${displayName(title)} right now`, {
      duration: 5000,
    });
    return false;
  }
  return pastePlaybook(title, label ? { id: pb.id, label } : pb, args);
}

/** Open a session's fork-icon menu (Ctrl+K F, the palette's Ask entry). The
 * menu hangs off the pane's own button, so the pane is brought forward first;
 * a CLI that gets no tools has no button, and says so instead. */
export function openPlaybookMenu(title: string, sub: "ask" | null = null): void {
  const inst = instances().find((r) => r.title === title);
  if (!inst || !mcpCapable(configCaps(), inst)) {
    toast(NO_TOOLS_REASON);
    return;
  }
  const why = forkBlockReason(inst);
  if (why) {
    toast(why, { duration: 5000 });
    return;
  }
  selectSession(title, { noKeyboard: true });
  // A frame later: the pane may have just been un-hidden, and its button is
  // the anchor the menu measures itself against.
  requestAnimationFrame(() => useUi.getState().setPlaybookMenu({ title, sub }));
}

/** "Queue prompt…": the Queue tab's own textarea, with the caret in it. The
 * tab renders "Loading…" until its first fetch lands, so wait for the box
 * (bounded) rather than focusing nothing. */
export function focusQueueInput(title: string): void {
  selectSession(title, { noKeyboard: true });
  useUi.getState().setLastTab(title, "queue");
  let tries = 0;
  const tick = () => {
    const pane = document.querySelector(`.pane[data-title="${CSS.escape(title)}"]`);
    const box = pane?.querySelector<HTMLTextAreaElement>(".pane-queue .queue-input");
    if (box) {
      box.focus();
      return;
    }
    if (++tries < 40) setTimeout(tick, 50);
  };
  setTimeout(tick, 0);
}

/** The config's caps for the non-React callers here — the query cache the
 * pane and the palette render from, no fetch of its own. */
function configCaps(): Partial<Caps> | undefined {
  return queryClient.getQueryData<Config>(["config"])?.caps;
}
