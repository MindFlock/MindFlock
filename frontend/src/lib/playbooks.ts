/** MindFlock MCP from the UI: who gets the agent tools, the New dialog's
 * split gate and its "suggested" pill, and the one remaining PASTE path.
 *
 * The row › menu, the palette and the ⏩ fast-track picker never paste
 * anything — they ACT, through lib/laneActions. What is left
 * here pastes only for the Thread tab's worker buttons and the rail's wrap-up
 * chip (`runPlaybook` / `pastePlaybook`), which the split runs replace once
 * the server merges pieces back itself.
 *
 * A paste is never a send: the server renders the prompt for this session
 * (`POST /api/playbooks/{id}/render`) and `/send {submit: false}` types it
 * into the agent's input box. Nothing runs until the user presses Enter.
 *
 * The pure half is unit-tested in node; the rest is a thin layer over the
 * API. */

import { api } from "../api/client";
import { childrenOf } from "./agentMessages";
import type { Caps, Instance, Playbook, PlaybooksResponse } from "../api/types";
import { displayName, useUi } from "../state/store";
import { errMsg } from "./format";
import { selectSession } from "./sessionActions";
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
      reason: "MindFlock tools are switched off for new sessions — Settings → Agent orchestration",
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
