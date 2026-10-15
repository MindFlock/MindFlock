/** Session actions shared by the sidebar ⋯ menu, keyboard shortcuts, the
 * command palette, and pane buttons (port of app.js section 10).
 *
 * Dialog-opening actions (commit/rename/device) go through the UI store; the
 * dialog components own their submit logic. */

import { ApiError, api, instApi } from "../api/client";
import type { Caps, Config, Instance } from "../api/types";
import { computeVisibleSlots } from "../components/grid/layout";
import { orderWithAfter } from "../components/sidebar/ordering";
import { patchInstance, queryClient, refreshInstances, refreshRecentlyClosed } from "../state/queries";
import { freshStage } from "./stageWatch";
import { displayName, effectiveViewMode, setSessionSelector, useUi, windowKey } from "../state/store";
import { toast } from "./toast";
import { errMsg } from "./format";
import { clearLoopReset, clearStep, markLoopReset, markStep } from "./stage";
import { focusTerm, releaseTerms } from "./terminals";
import { confirmPop, errorPop } from "./errorPop";

function caps() {
  return queryClient.getQueryData<Config>(["config"])?.caps ?? {
    git: true,
    tailscale: true,
    ticketing: true,
    github: true,
  };
}

function ideName() {
  return queryClient.getQueryData<Config>(["config"])?.ide_name || "Cursor";
}

export function instances(): Instance[] {
  return queryClient.getQueryData<Instance[]>(["instances"]) || [];
}

/** Select a session — the unified "work on this one now" action. Marks it
 * most-recently-used, unhides it, and focuses it; the sidebar order is NOT
 * touched. When the grid is full (capped view) and the session has no pane
 * yet, it takes the FOCUSED window's exact spot (the focused one is demoted
 * below every other visible window in the MRU, making it the eviction
 * candidate) rather than evicting whichever window is least-recently-used. */
export function selectSession(title: string, opts?: { noKeyboard?: boolean }) {
  const ui = useUi.getState();
  ui.setHidden(title, false);
  // Visibility is judged over the UNION the grid actually renders — sessions
  // AND open windows, one cap, one MRU (computeVisibleSlots, exactly as
  // TerminalGrid calls it). Judging with sessions-only computeVisible
  // over-reported: a session whose slot a window held counted as visible, so
  // the demotion below promoted that phantom and shoved the on-screen window
  // under the demoted focused session — selecting a session evicted the
  // window you had just picked. The keys must cover every open window kind
  // (the same three lists windowRows in sidebar/WindowList.tsx renders).
  const windowKeys = [
    ...ui.specialOpen.map((k) => windowKey(k)),
    ...ui.verifyPanes.map((t) => windowKey("verify", t)),
    ...ui.extPanes.map((p) => windowKey("ext", p.key)),
  ];
  const visible = computeVisibleSlots(instances(), windowKeys, {
    hidden: ui.hidden,
    viewMode: effectiveViewMode(ui),
    mru: ui.mru,
    order: ui.order,
  });
  const focused = ui.focused;
  if (!visible.includes(title) && focused && focused !== title && visible.includes(focused)) {
    const keep = visible.filter((t) => t !== focused);
    const keepMru = ui.mru.filter((t) => keep.includes(t));
    const keepRest = keep.filter((t) => !keepMru.includes(t));
    ui.setOrderlessMru(
      keepMru.concat(keepRest, [focused], ui.mru.filter((t) => t !== focused && !keep.includes(t)))
    );
  }
  ui.touchMru(title);
  ui.setFocused(title);
  if (!opts?.noKeyboard) focusTerm(title);
}
// The store's own actions that bring a session forward (threadOpen) go through
// this function too, so there is one selection rule, not a second copy of it.
setSessionSelector(selectSession);

/** Select a non-session window by its grid sentinel: top of the MRU, so a
 * capped view ("1", "2", "4") gives it a slot — the same thing selecting a
 * session does — and then bring it on screen. Deliberately NOT `setFocused`:
 * focus is the KEYBOARD target and the shortcuts behind it (Ctrl+W closes the
 * focused session) only mean anything for a session.
 *
 * The scroll is best-effort and NOT a CSS attribute selector: the sentinel
 * starts with U+0000, which CSS.escape must replace with U+FFFD (per spec), so
 * a selector can never match it — compare the attribute value directly. */
export function selectWindow(sent: string) {
  useUi.getState().touchMru(sent);
  // After the store update the pane may not exist yet (it was outside the cap
  // a moment ago), so look for it on the next frame.
  requestAnimationFrame(() => {
    for (const pane of document.querySelectorAll(".pane")) {
      if (pane.getAttribute("data-title") === sent) {
        pane.scrollIntoView({ block: "nearest", behavior: "smooth" });
        return;
      }
    }
  });
}

/** Select any rail row by its order key — a session title, or a window's
 * NUL-prefixed sentinel. What Alt+N / Ctrl+N / Ctrl+Tab dispatch through:
 * the rail numbers sessions and windows in one list, so its shortcuts have
 * to land on either kind. */
export function selectRailKey(key: string) {
  if (key.startsWith("\u0000")) selectWindow(key);
  else selectSession(key);
}

export function requireGit(): boolean {
  if (caps().git) return true;
  toast("git is not installed — install git to use diffs, commits and PRs");
  return false;
}

// --- PR support: gh is optional, never required ------------------------------
// Pushing is always plain `git push` over whatever remote (SSH or HTTPS) the
// user configured — MindFlock is never in that path. Only *opening* and
// *merging* a PR need GitHub credentials, and even then the server degrades to
// handing back a prefilled GitHub URL. So "no PR support" is a detour sign, not
// a wall: every entry point stays clickable and ends on github.com.

/** The one remedy sentence. Asserted verbatim in docs and tests — if you
 * change it, change it there too. */
export const PR_REMEDY =
  "add a GitHub token in Settings → Connections → GitHub → Connect, or install the GitHub CLI";

/** Tooltip for a PR/Merge affordance that will fall back to the browser. */
export const PR_FALLBACK_HINT =
  "MindFlock can’t open or merge the PR for you yet — " +
  PR_REMEDY +
  ".\nThis still works: it opens GitHub’s prefilled page in your browser instead.";

/** POST /make-pr — 200 either way. `ok: false` carries the browser fallback. */
interface MakePrResult {
  ok?: boolean;
  url?: string;
  compare_url?: string;
  message?: string;
}

/** POST /merge-pr — 200 either way; `pr_url` is where to merge by hand. */
interface MergePrResult {
  ok?: boolean;
  pr_url?: string;
  message?: string;
}

/** True when the server can open/merge PRs itself (gh authenticated OR a
 * GitHub token resolves). Feature-detected against an explicit `false` so an
 * older server that never reports the capability is assumed capable and
 * nothing regresses. */
export function hasPrSupport(c?: Partial<Caps>): boolean {
  return (c ?? caps()).github !== false;
}

/** Open `url` in a new tab, and if the popup blocker ate it (we are in an
 * async continuation, not a click handler) fall back to a clickable toast —
 * the user's click then counts as the gesture. Never blocks the UI. */
export function offerUrl(url: string, msg: string) {
  const win = window.open(url, "_blank");
  if (win) {
    toast(msg, { duration: 6000 });
    return;
  }
  toast(msg + " — click to open", {
    onClick: () => window.open(url, "_blank"),
    duration: 9000,
  });
}

/** Stack of sessions closed this run (newest last) so Ctrl+Shift+T reopens
 * exactly what you closed. */
const closeUndo: string[] = [];

/* --- Ending a session -------------------------------------------------------
 * Exactly two verbs, everywhere (row menu, ✕, Ctrl+W, palette, bulk bar):
 *   Close   POST /close    — stops the agent and moves the session to
 *                            Recently closed (worktree kept); reopen it there.
 *   Delete  POST /cleanup  — removes the worktree and its branch. Asked first,
 *                            inline (the row's confirm strip, or a neutral card).
 * Both answer `{title, wiped, children}`; `children` are the worker sessions
 * that stay running without their parent. Delete (and the bare DELETE that
 * Clean up uses for a gone workspace) answers 409 "folder in use" when another
 * live session shares the folder — a Duplicate session runs in place — and
 * that is a thing to say, not a failure to swallow. */

/** What /close, /cleanup and DELETE answer. Older servers answered `{ok}`;
 * every field is read defensively. */
export interface EndResult {
  title?: string;
  wiped?: boolean;
  children?: string[];
}

/** The live sessions a 409 "folder in use" names, or null for any other error. */
export function folderInUse(err: unknown): string[] | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const body = err.body as { error?: unknown; sessions?: unknown } | null;
  if (!body || body.error !== "folder in use") return null;
  return Array.isArray(body.sessions) ? body.sessions.map(String) : [];
}

/** "Folder in use by a, b. Close those first." — one sentence, shared by the
 * row's inline card, the corner card and the bulk bar. */
export function folderInUseText(titles: string[]): string {
  const names = titles.map((t) => displayName(t));
  return "Folder in use by " + (names.length ? names.join(", ") : "another session") + ". Close those first.";
}

/** "3 worker sessions kept running" — the after-note for an end whose
 * children stayed up; null when there were none. */
export function keptRunningText(result: EndResult | null | undefined): string | null {
  const n = result?.children?.length || 0;
  if (!n) return null;
  return n === 1 ? "1 worker session kept running" : `${n} worker sessions kept running`;
}

/** The local bookkeeping every end shares: unhide, unfocus, drop the terminals. */
function forgetLocally(title: string) {
  const ui = useUi.getState();
  ui.setHidden(title, false);
  if (ui.focused === title) ui.setFocused(null);
  releaseTerms(title);
}

/** Close: stop the agent and move the session to Recently closed (its
 * worktree is kept); reopen it from there. Not Pause, which leaves the
 * session in the sidebar. */
export async function closeSession(title: string) {
  if (!title) return;
  let r: EndResult | null = null;
  try {
    r = await instApi<EndResult>(title, "/close", { method: "POST" });
  } catch (err) {
    errorPop("Close failed", errMsg(err));
    return;
  }
  forgetLocally(title);
  closeUndo.push(title);
  const kept = keptRunningText(r);
  // The toast carries the Undo: its button reopens exactly this session (the
  // same path as Ctrl+Shift+T), and it stays up long enough to be pressed.
  toast(closedToastText(displayName(title), kept), {
    action: { label: "Undo", onClick: () => void undoLastClose(), ariaLabel: "Undo closing “" + displayName(title) + "”" },
    title: CLOSED_UNDO_HINT,
    duration: 7000,
  });
  void refreshRecentlyClosed();
  await refreshInstances();
}

/** The Close toast's words: what closed and where it lives afterwards. Its
 * Undo is a button beside them, and the keyboard road is the tooltip. */
export function closedToastText(name: string, kept?: string | null): string {
  return "Closed “" + name + "”" + (kept ? " · " + kept : "");
}

/** The Close toast's tooltip: the Undo button, and its shortcut. */
export const CLOSED_UNDO_HINT = "Reopen it — click to Undo (Ctrl+Shift+T)";

/** Delete's question, wherever it is asked (the menu's inline strip, the
 * corner card): what goes, that uncommitted work goes with it, and that Close
 * is the keeping alternative. */
export function deleteConfirmText(inst: { branch?: string; in_place?: boolean } | undefined): string {
  if (inst?.in_place)
    return "Removes this session. Its folder and branch stay as they are; Close keeps the session to reopen.";
  const branch = inst?.branch ? " “" + inst.branch + "”" : "";
  return (
    "Deletes the folder and branch" +
    branch +
    ", including uncommitted changes. This can't be undone — use Close to keep it."
  );
}

/** Delete: remove the worktree and the branch. Asks first unless `confirmed`
 * — the row's menu asks on its own inline strip and passes true; the palette
 * and the pane go through the neutral corner card here.
 *
 * Resolves to the sessions holding the folder on a 409 (the caller draws
 * "Folder in use by …" where its button is), `"done"` after a delete, and
 * `null` for anything else (already reported on a card, or not confirmed). */
export async function deleteSession(
  title: string,
  confirmed = false
): Promise<"done" | { inUse: string[] } | null> {
  if (!title) return null;
  if (!confirmed) {
    confirmPop(
      "Delete " + displayName(title) + "?",
      deleteConfirmText(instances().find((i) => i.title === title)),
      [
        {
          label: "Delete",
          primary: true,
          run: () =>
            void deleteSession(title, true).then((r) => {
              if (r && r !== "done") errorPop("Can't delete " + displayName(title), folderInUseText(r.inUse));
            }),
        },
      ]
    );
    return null;
  }
  let r: EndResult | null = null;
  try {
    r = await instApi<EndResult>(title, "/cleanup", { method: "POST" });
  } catch (err) {
    const held = folderInUse(err);
    if (held) return { inUse: held };
    errorPop("Delete failed", errMsg(err));
    return null;
  }
  forgetLocally(title);
  const kept = keptRunningText(r);
  toast(
    "Deleted " + displayName(title) + " — worktree and branch removed" + (kept ? " · " + kept : ""),
    kept ? { duration: 5000 } : undefined
  );
  await refreshInstances();
  return "done";
}

interface ClosedEntry {
  id: string;
  title: string;
  closed_at: number | string;
  exists: boolean;
}

const closedWhen = (x: ClosedEntry) => {
  const n = typeof x.closed_at === "number" ? x.closed_at : Date.parse(x.closed_at);
  return isNaN(n) ? 0 : n;
};

let undoBusy = false;

/** Ctrl+Shift+T: reopen exactly the session you last closed; fall back to the
 * newest reopenable entry. A gone worktree is reported, not skipped over. */
export async function undoLastClose() {
  if (undoBusy) return;
  undoBusy = true;
  try {
    let list: ClosedEntry[];
    try {
      const data = await api<ClosedEntry[]>("/api/recently-closed");
      list = Array.isArray(data) ? data : [];
    } catch {
      return;
    }
    if (!list.length) {
      toast("Nothing to reopen");
      return;
    }
    let ent: ClosedEntry | null = null;
    while (closeUndo.length && !ent) {
      const t = closeUndo.pop();
      const matches = list
        .filter((e) => e.title === t)
        .sort((a, b) => closedWhen(b) - closedWhen(a));
      if (matches.length) ent = matches[0];
    }
    if (!ent) ent = [...list].sort((a, b) => closedWhen(b) - closedWhen(a))[0];
    if (!ent) {
      toast("Nothing to reopen");
      return;
    }
    if (!ent.exists) {
      toast("Can’t reopen “" + (ent.title ? displayName(ent.title) : "session") + "” — its worktree is gone", {
        duration: 5000,
      });
      return;
    }
    try {
      const inst = await api<Instance>(
        "/api/recently-closed/" + encodeURIComponent(ent.id) + "/reopen",
        { method: "POST" }
      );
      void refreshRecentlyClosed();
      await refreshInstances();
      if (inst?.title) selectSession(inst.title);
      toast("Reopened " + (ent.title ? displayName(ent.title) : "session"));
    } catch (err) {
      errorPop("Reopen failed", errMsg(err));
    }
  } finally {
    undoBusy = false;
  }
}

/** L7: remove a session whose workspace directory vanished. Asks first on a
 * neutral card (the desktop app has no confirm(): it silently meant "no", so
 * Clean up did nothing there); the card's button re-enters `confirmed`. */
export async function cleanupMissing(title: string, confirmed = false) {
  if (!title) return;
  if (!confirmed) {
    confirmPop(
      "Delete “" + displayName(title) + "”?",
      "Its workspace folder no longer exists — this removes the dead session.",
      [{ label: "Delete", primary: true, run: () => void cleanupMissing(title, true) }]
    );
    return;
  }
  const ui = useUi.getState();
  ui.setHidden(title, false);
  if (ui.focused === title) ui.setFocused(null);
  // Optimistic: drop the row NOW (the DELETE can take seconds).
  queryClient.setQueryData<Instance[]>(["instances"], (prev) =>
    (prev || []).filter((i) => i.title !== title)
  );
  releaseTerms(title);
  let r: EndResult | null = null;
  try {
    r = await instApi<EndResult>(title, "", { method: "DELETE" });
  } catch (err) {
    const held = folderInUse(err);
    if (held) errorPop("Can't delete " + displayName(title), folderInUseText(held));
    else errorPop("Delete failed", errMsg(err));
    await refreshInstances(); // restore the row — the session still exists
    return;
  }
  const kept = keptRunningText(r);
  toast("Session removed" + (kept ? " · " + kept : ""), kept ? { duration: 5000 } : undefined);
  await refreshInstances();
}

/** A brand-new session must not wear a dead one's name.
 *
 * Renames are client-side aliases keyed by title, and they deliberately OUTLIVE
 * the session — a closed session can be reopened (Ctrl+Shift+T) and should come
 * back with the name the user gave it. The cost is that a REUSED title inherits
 * the alias: duplicating hit this every time, because the second `foo-copy`
 * picked up whatever the first `foo-copy` had been renamed to, and the new row
 * read as some unrelated session from last week. So creating a title drops any
 * alias left over for it. */
export function clearStaleAlias(title: string) {
  const ui = useUi.getState();
  if (title && ui.aliases[title]) ui.setAlias(title, "");
}

/** Optimistic "provisioning" row for create/duplicate (mirrors the server's
 * _unique_title numbering; a mismatch resolves on the next poll). */
export function addPendingSession(bare: string, device = ""): string {
  // A session being started on another device waits in THAT device's group,
  // under the namespaced title it will arrive with.
  const base = device ? device + "::" + bare : bare;
  const taken = new Set(instances().map((i) => i.title));
  let title = base;
  if (taken.has(base)) {
    let i = 2;
    while (taken.has(base + "-" + i)) i++;
    title = base + "-" + i;
  }
  clearStaleAlias(title);
  const pending = {
    title,
    status: "loading",
    pending_create: true,
    ...(device ? { device, display_title: title.slice(device.length + 2) } : {}),
  } as unknown as Instance;
  queryClient.setQueryData<Instance[]>(["instances"], (prev) => [...(prev || []), pending]);
  return title;
}

export function failPendingSession(title: string) {
  queryClient.setQueryData<Instance[]>(["instances"], (prev) =>
    (prev || []).filter(
      (i) => !((i as unknown as { pending_create?: boolean }).pending_create && i.title === title)
    )
  );
  // A duplicate reserves the guessed title a slot in the sidebar order so the
  // row doesn't land at the bottom and then jump. If the create failed there
  // is no session to hold it — and the name is one the NEXT duplicate of the
  // same window will be handed, which would inherit a slot it never earned.
  dropFromOrder(title);
}

/** Forget a title that never became a session. */
function dropFromOrder(title: string) {
  const ui = useUi.getState();
  if (title && ui.order.includes(title)) ui.setOrder(ui.order.filter((t) => t !== title));
}

/** Put a freshly created session directly beneath the one it came from.
 *
 * The saved order is sparse — it only holds what has been dragged or placed —
 * so it is merged with the live list first, into exactly the arrangement
 * `orderedInstances` renders: saved slots in their saved order, then everything
 * it has never seen in server order.
 *
 * Deliberately a MERGE rather than that function's `nextOrder`. nextOrder is
 * the live list and nothing else, so persisting it would quietly erase the
 * saved slot of every session missing from the current snapshot — a remote
 * device that happens to be asleep, for one, whose rows would then come back at
 * the bottom in server order with nothing the user did to explain it.
 *
 * Both titles have to be live. A remote row's copy is answered by the device
 * that owns it, under its own BARE title rather than the `device::title` the
 * rail shows, and placing that would persist a slot for a session this browser
 * has never seen. */
function placeAfter(title: string, after: string) {
  const ui = useUi.getState();
  const live = instances().map((i) => i.title);
  if (!live.includes(title) || !live.includes(after)) return;
  const seen = new Set(ui.order);
  const merged = ui.order.concat(live.filter((t) => !seen.has(t)));
  const next = orderWithAfter(merged, title, after);
  if (next !== merged) ui.setOrder(next);
}

/** Copy a window: a second session sharing this one's worktree.
 *
 * The copy lands directly under its source, twice: once for the optimistic
 * provisioning row, so it doesn't appear at the bottom and then jump, and
 * again for the real title the server picked, which can differ from the guess. */
export async function copySession(title: string) {
  if (!title) return;
  const guess = addPendingSession(title + "-copy");
  placeAfter(guess, title);
  try {
    const inst = await instApi<Instance>(title, "/copy", { method: "POST" });
    // The server picks the real title (its own -copy/-copy-2 numbering), which
    // can differ from the optimistic guess — so clear that one's alias too.
    if (inst?.title) clearStaleAlias(inst.title);
    await refreshInstances();
    if (inst?.title) {
      // The guessed title reserved a slot; if the server picked a different
      // one, that slot belongs to nothing.
      if (inst.title !== guess) dropFromOrder(guess);
      placeAfter(inst.title, title);
      selectSession(inst.title);
    }
  } catch (err) {
    failPendingSession(guess);
    errorPop("Copy failed", errMsg(err));
  }
}

/** The body of POST /branch: the new row plus how it was cut. */
interface BranchResult extends Instance {
  branched_from?: string;
  base_branch?: string;
  /** The source had uncommitted changes and they came along. */
  carried?: boolean;
}

/** Branch a session: a NEW session in its own worktree, on a new branch cut
 * from this one's current HEAD, carrying a copy of its uncommitted changes
 * (the source is untouched). Lands under its source and takes focus, the
 * same way a Duplicate does; a refusal (not a git repo, still starting, …) is
 * an error card with the server's reason. */
export async function branchSession(title: string) {
  if (!title) return;
  const guess = addPendingSession(title + "-branch");
  placeAfter(guess, title);
  try {
    const inst = await instApi<BranchResult>(title, "/branch", { method: "POST" });
    if (inst?.title) clearStaleAlias(inst.title);
    await refreshInstances();
    if (inst?.title) {
      if (inst.title !== guess) dropFromOrder(guess);
      placeAfter(inst.title, title);
      selectSession(inst.title);
      toast(
        `Branched ${displayName(title)} → ${inst.title}` +
          (inst.carried ? " with its uncommitted changes" : ""),
        { duration: 4000 }
      );
    }
  } catch (err) {
    failPendingSession(guess);
    errorPop("Branch failed", errMsg(err));
  }
}

/** Commit… — the table's Commit action (lib/sessionActionTable): opens the
 * Commit dialog, whose submit calls commitNow. */
export function commitSession(title: string) {
  if (!requireGit() || !title) return;
  useUi.getState().openDialogFor("commit", title);
}

/** POST /commit with the typed message. `proceedAnyway` is the second click
 * past a red-zone warning (zones warn at a commit, they never block it). */
export async function commitNow(title: string, message: string, proceedAnyway = false): Promise<void> {
  await instApi(title, "/commit", { json: { message, ...(proceedAnyway ? { proceed_anyway: true } : {}) } });
}

/* --- Red-zone gate ----------------------------------------------------------
 * push-branch / make-pr / merge-pr refuse with 409 when the branch carries
 * changes inside a red zone (`red_zone_breaches` in the body). The override is
 * an explicit second click on an inline card — never confirm(), which the
 * Electron app does not implement (it would silently mean "no"). */

export interface RedZoneBreachHit {
  path: string;
  pattern: string;
  zone_id?: string | null;
  /** "green" = changed outside the session's green (only-here) scope. */
  kind?: string;
}

/** The breaches a red-zone 409 carries, or null for any other failure. */
export function redZoneBreaches(err: unknown): RedZoneBreachHit[] | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const body = err.body as { red_zone_breaches?: unknown } | null;
  const list = body && Array.isArray(body.red_zone_breaches) ? (body.red_zone_breaches as RedZoneBreachHit[]) : null;
  return list && list.length ? list : null;
}

/** Explain the block and offer the two ways out: look at it on the map, or do
 * it anyway for this one action. */
export function offerRedZoneOverride(
  title: string,
  verb: "Push" | "Open PR" | "Merge",
  breaches: RedZoneBreachHit[],
  retry: () => void
) {
  const n = breaches.length;
  const shown = breaches
    .slice(0, 5)
    .map((b) => b.path + (b.pattern && b.pattern !== b.path ? " (" + b.pattern + ")" : ""))
    .join(" · ");
  // A green (only-here) breach is a file changed OUTSIDE the scope, not a
  // protected file — say which one it is.
  const green = breaches.every((b) => b.kind === "green");
  errorPop(
    green ? `${verb} blocked — changes outside the green zone` : `${verb} blocked — red zone changed`,
    `${displayName(title)} changed ${n} ${green ? "file" + (n === 1 ? "" : "s") + " outside its scope" : "protected file" + (n === 1 ? "" : "s")}: ${shown}` +
      (n > 5 ? ` · and ${n - 5} more` : "") +
      ". Revert them (the agent can: “revert your changes to …”), or override for this " +
      verb.toLowerCase() +
      " only.",
    [
      {
        label: "Open map",
        run: () => {
          selectSession(title);
          useUi.getState().setLastTab(title, "map");
        },
      },
      {
        label: verb + " anyway",
        primary: true,
        title: "Override the zone gate for this one " + verb.toLowerCase(),
        run: retry,
      },
    ]
  );
}

/** The push route's 409 `diverged` answer — the remote branch moved on (a
 * teammate, or you on another machine, pushed to it) — or null. */
export interface DivergedPush {
  error: string;
  remote: string;
  branch: string;
  behind: number;
  authors: string[];
}

export function divergedPush(err: unknown): DivergedPush | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const b = err.body as Partial<DivergedPush> & { diverged?: unknown } | null;
  if (!b || typeof b !== "object" || b.diverged !== true) return null;
  return {
    error: String(b.error || err.message),
    remote: String(b.remote || "origin"),
    branch: String(b.branch || ""),
    behind: Number(b.behind) || 0,
    authors: Array.isArray(b.authors) ? b.authors.map(String) : [],
  };
}

/** The diverged card's "Rebase onto <remote>/<branch> and push": POST
 * /rebase-remote (git pull --rebase; on a conflict the server aborts and the
 * tree is as it was), then the same push again. Never a force-push. */
export async function rebaseRemoteAndPush(
  title: string,
  remote: string,
  force = false,
  overrideRedZones = false
): Promise<void> {
  try {
    const r = await instApi<{ row?: Instance | null }>(title, "/rebase-remote", { json: { remote } });
    if (r?.row?.title) patchInstance(title, r.row);
  } catch (err) {
    errorPop("Rebase stopped — nothing changed", displayName(title) + ": " + errMsg(err));
    void freshStage(title);
    return;
  }
  await pushSession(title, force, overrideRedZones);
}

export async function pushSession(title: string, force = false, overrideRedZones = false): Promise<void> {
  if (!title || !requireGit()) return;
  selectSession(title, { noKeyboard: true });
  // Push/PR/merge have no stage of their own until the RESULT is observable, so
  // without this marker the header showed the previous step for seconds and people
  // pressed the button twice.
  markStep(title, "push");
  try {
    const body: Record<string, unknown> = {};
    if (force) body.force = true;
    if (overrideRedZones) body.override_red_zones = true;
    await instApi(title, "/push-branch", { json: body });
  } catch (err) {
    const rz = redZoneBreaches(err);
    const div = divergedPush(err);
    if (div) {
      // Someone else pushed to this branch: offer to replay this session's
      // commits on top of theirs, then push. The card names the session (the
      // same dedupe reason as the check gate's below).
      clearStep(title);
      const where = div.remote + "/" + div.branch;
      confirmPop(
        "Push blocked — " + where + " moved on",
        displayName(title) + ": " + div.error + ". Rebase its commits onto " + where + " and push?",
        [
          {
            label: "Rebase onto " + where + " and push",
            primary: true,
            title: "git pull --rebase " + div.remote + " " + div.branch + ", then push (never a force-push)",
            run: () => void rebaseRemoteAndPush(title, div.remote, force, overrideRedZones),
          },
        ],
        "Cancel"
      );
    } else if (rz) {
      clearStep(title);
      offerRedZoneOverride(title, "Push", rz, () => void pushSession(title, force, true));
      // Falls through to freshStage below: that re-read is what repaints the
      // header off the cleared "pushing" marker.
    } else if ((err as Error).message === "checks haven't passed for this commit") {
      // O3 soft gate: checks haven't passed — offer an explicit override on
      // an inline card (confirm() is a silent "no" in the desktop app). A
      // red-zone override already given still applies to the re-push.
      // The card names its session: errorPop drops a card whose title+detail
      // is already on screen, so a nameless one meant a second blocked push
      // reused the first card — and its Push anyway pushed the OTHER session.
      clearStep(title);
      errorPop(
        "Push blocked — checks haven't passed",
        displayName(title) + "'s commit has no passing check run (see the ✗ checks chip on its row). Override for this push only?",
        [
          {
            label: "Push anyway",
            primary: true,
            title: "Push " + displayName(title) + " past the check gate",
            run: () =>
              void (overrideRedZones ? pushSession(title, true, true) : pushSession(title, true)),
          },
        ]
      );
      return;
    } else {
      clearStep(title);
      errorPop("Push failed", errMsg(err));
    }
  }
  // The old `setTimeout(refreshInstances, 1000)` could not observe anything: the
  // server serves GET /api/instances from its tick snapshot for up to 10s. Read
  // the one session fresh instead — and the server's push watcher republishes the
  // moment the branch reaches origin, so "Make PR" appears without another poll.
  void freshStage(title);
}

/** POST /reset-stage — the ↺ control: put this window back to idle.
 *
 * Nothing git-facing happens; the server records a display pin it releases as
 * soon as the worktree moves, and hands back the recomputed row. The local echo
 * (markLoopReset) flips the header on the press so the click feels immediate,
 * and expires by itself if the request never lands.
 *
 * `cleared` names the previous cycle's leftovers the server took down with it
 * (a halted fast-track, a stale check result) — worth saying out loud, since
 * they are badges the user can see disappear. */
export async function resetStage(title: string, opts?: { quiet?: boolean }) {
  if (!title || !requireGit()) return;
  markLoopReset(title);
  try {
    // `method` is NOT optional here: api() only upgrades to POST when a `json`
    // body is passed, and this route needs none — omitting it sent a GET, which
    // the static fallback answers with a 404 ("Not Found") rather than a 405.
    const r = await instApi<{ row?: Instance | null; cleared?: string[] }>(
      title,
      "/reset-stage",
      { method: "POST" }
    );
    if (r?.row?.title) patchInstance(title, r.row);
    const cleared = r?.cleared || [];
    // `quiet` is for the automatic reset that follows a successful Make PR: the
    // user just got a PR tab and a toast about it, and a second toast narrating
    // a header that reset itself is noise.
    if (!opts?.quiet)
      toast(
        "Back to idle" + (cleared.length ? " — also cleared " + cleared.join(" + ") : "")
      );
  } catch (err) {
    clearLoopReset(title);
    toast("Could not reset this session: " + errMsg(err), { duration: 6000 });
  }
}

export async function ideSession(title: string, quiet = false) {
  if (!title) return;
  try {
    await instApi(title, "/ide", { method: "POST" });
  } catch (err) {
    if (quiet) toast(ideName() + ": " + errMsg(err), { duration: 7000 });
    else errorPop(ideName() + " failed", errMsg(err));
  }
}

/** Open the Make-PR dialog (branch picker). The dialog calls submitMakePr with
 * the chosen base — so every entry point (button, palette, keymap, stage pill)
 * asks which branch to merge into first. */
export function makePrSession(title: string) {
  if (!title || !requireGit()) return;
  useUi.getState().openDialogFor("make-pr", title);
}

/** Actually open the PR into `base` (empty = let the server decide). Called by
 * the Make-PR dialog once the user picks a branch.
 *
 * The server always answers 200. `ok: false` is the "I pushed your branch but
 * I can't file the PR for you" case (no gh, no token): it comes with a
 * prefilled `compare_url`, so we send the user straight there rather than
 * showing them a modal about a CLI they never asked for. */
export async function submitMakePr(title: string, base: string, overrideRedZones = false) {
  if (!title || !requireGit()) return;
  markStep(title, "pr");
  try {
    const body: Record<string, unknown> = {};
    if (base) body.base = base;
    if (overrideRedZones) body.override_red_zones = true;
    const r = await instApi<MakePrResult>(title, "/make-pr", { json: body });
    if (r && r.ok === false) {
      const msg = r.message || PR_REMEDY;
      if (r.compare_url) offerUrl(r.compare_url, "Opened GitHub’s compare page — " + msg);
      else toast(msg, { duration: 9000 });
    } else {
      if (r?.url) window.open(r.url, "_blank");
      // PR is open — restart the guided cycle: put the pill back to idle so the
      // button reads "Commit…" again and the commit→push→PR loop can repeat in
      // this session. Server-side (the same route the ↺ button uses), so it also
      // survives a reload and reaches /m; it self-clears once the worktree moves.
      await resetStage(title, { quiet: true });
    }
  } catch (err) {
    clearStep(title);
    const rz = redZoneBreaches(err);
    if (rz) offerRedZoneOverride(title, "Open PR", rz, () => void submitMakePr(title, base, true));
    else errorPop("Open PR failed", errMsg(err));
  }
  await freshStage(title);
}

/** Merge the branch's PR. Same shape as make-pr: `ok: false` + `pr_url` means
 * "merge it yourself on GitHub", which is a link, not a failure. */
export async function mergeSession(title: string, overrideRedZones = false, confirmed = false) {
  if (!title || !requireGit()) return;
  // Asked on a neutral inline card (confirm() is a silent "no" in the desktop
  // app, so Merge did nothing there); its button re-enters `confirmed`. The
  // red-zone override re-entry skips the question: its click on the card IS
  // the answer.
  if (!overrideRedZones && !confirmed) {
    confirmPop(
      "Merge this PR?",
      displayName(title) + "'s pull request merges on GitHub — this can't be undone from MindFlock.",
      [{ label: "Merge PR", primary: true, run: () => void mergeSession(title, false, true) }]
    );
    return;
  }
  markStep(title, "merge");
  try {
    const r = await instApi<MergePrResult>(
      title,
      "/merge-pr",
      overrideRedZones ? { json: { override_red_zones: true } } : { method: "POST" }
    );
    if (r && r.ok === false) {
      const msg = r.message || PR_REMEDY;
      if (r.pr_url) offerUrl(r.pr_url, "Opened the PR on GitHub to merge there — " + msg);
      else toast(msg, { duration: 9000 });
    }
  } catch (err) {
    clearStep(title);
    const rz = redZoneBreaches(err);
    if (rz) offerRedZoneOverride(title, "Merge", rz, () => void mergeSession(title, true));
    else errorPop("Merge failed", errMsg(err));
  }
  await freshStage(title);
}

/** Hide/show a session's pane (client-side only; the session keeps running). */
export function hideSession(title: string) {
  if (!title) return;
  const ui = useUi.getState();
  if (ui.hidden.has(title)) {
    ui.setHidden(title, false);
    return;
  }
  ui.setHidden(title, true);
  if (ui.focused === title) ui.setFocused(null);
  toast("Hid " + (ui.aliases[title] || title) + " — click to undo", {
    onClick: () => useUi.getState().setHidden(title, false),
    duration: 5000,
  });
}

/** Pause: stops the agent and the session STAYS in the sidebar (status
 * paused; no commit, nothing removed). Resume starts the agent again in the
 * same worktree. Close is the other stop: it moves the session to Recently
 * closed. */
export async function pauseSession(title: string) {
  if (!title) return;
  try {
    await instApi(title, "/pause", { method: "POST" });
  } catch (err) {
    errorPop("Pause failed", errMsg(err));
  }
  await refreshInstances();
}

export async function resumeSession(title: string) {
  if (!title) return;
  try {
    await instApi(title, "/resume", { method: "POST" });
  } catch (err) {
    errorPop("Resume failed", errMsg(err));
  }
  await refreshInstances();
}
