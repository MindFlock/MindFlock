/** Session actions shared by the sidebar ⋯ menu, keyboard shortcuts, the
 * command palette, and pane buttons (port of app.js section 10).
 *
 * Dialog-opening actions (commit/rename/device) go through the UI store; the
 * dialog components own their submit logic. */

import { api, instApi } from "../api/client";
import type { AutopilotRun, Caps, Config, Instance } from "../api/types";
import { computeVisibleSlots } from "../components/grid/layout";
import { orderWithAfter } from "../components/sidebar/ordering";
import { patchInstance, queryClient, refreshInstances } from "../state/queries";
import { freshStage } from "./stageWatch";
import { displayName, useUi, windowKey } from "../state/store";
import { toast } from "./toast";
import { errMsg } from "./format";
import { clearLoopReset, clearStep, markLoopReset, markStep } from "./stage";
import { depthLabel, normalizeDepth } from "./autopilot";
import { focusTerm, releaseTerms } from "./terminals";

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
    viewMode: ui.viewMode,
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
  "add a GitHub token in Intake → Pull requests, or install the GitHub CLI";

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

export async function killSession(title: string) {
  if (!title) return;
  try {
    await instApi(title, "/close", { method: "POST" });
  } catch (err) {
    alert("Close failed: " + errMsg(err));
    return;
  }
  const ui = useUi.getState();
  ui.setHidden(title, false);
  if (ui.focused === title) ui.setFocused(null);
  releaseTerms(title);
  closeUndo.push(title);
  toast("Session ended — reopen from Recent… (or Ctrl+Z / Ctrl+Shift+T)");
  await refreshInstances();
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
      await refreshInstances();
      if (inst?.title) selectSession(inst.title);
      toast("Reopened " + (ent.title ? displayName(ent.title) : "session"));
    } catch (err) {
      toast("Reopen failed: " + errMsg(err), { duration: 5000 });
    }
  } finally {
    undoBusy = false;
  }
}

/** L7: remove a session whose workspace directory vanished. */
export async function cleanupMissing(title: string) {
  if (!title) return;
  if (
    !confirm(
      "Clean up '" +
        title +
        "'?\nIts workspace directory no longer exists — this removes the dead session."
    )
  )
    return;
  const ui = useUi.getState();
  ui.setHidden(title, false);
  if (ui.focused === title) ui.setFocused(null);
  // Optimistic: drop the row NOW (the DELETE can take seconds).
  queryClient.setQueryData<Instance[]>(["instances"], (prev) =>
    (prev || []).filter((i) => i.title !== title)
  );
  releaseTerms(title);
  try {
    await instApi(title, "", { method: "DELETE" });
  } catch (err) {
    toast("Clean up failed: " + errMsg(err), { duration: 5000 });
    await refreshInstances(); // restore the row — the session still exists
    return;
  }
  toast("Session removed");
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
export function addPendingSession(base: string): string {
  const taken = new Set(instances().map((i) => i.title));
  let title = base;
  if (taken.has(base)) {
    let i = 2;
    while (taken.has(base + "-" + i)) i++;
    title = base + "-" + i;
  }
  clearStaleAlias(title);
  const pending = { title, status: "loading", pending_create: true } as unknown as Instance;
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
    alert("Copy failed: " + errMsg(err));
  }
}

export function commitSession(title: string) {
  if (!requireGit() || !title) return;
  useUi.getState().openDialogFor("commit", title);
}

export async function pushSession(title: string, force = false) {
  if (!title || !requireGit()) return;
  selectSession(title, { noKeyboard: true });
  // Push/PR/merge have no stage of their own until the RESULT is observable, so
  // without this marker the header showed the previous step for seconds and people
  // pressed the button twice.
  markStep(title, "push");
  try {
    await instApi(title, "/push-branch", { json: force ? { force: true } : {} });
  } catch (err) {
    // O3 soft gate: checks haven't passed — offer an explicit override.
    if ((err as Error).message === "checks haven't passed for this commit") {
      if (
        confirm("Checks haven't passed for this commit (see the ✗ checks chip).\nPush anyway?")
      )
        return pushSession(title, true);
      clearStep(title);
      return;
    }
    clearStep(title);
    toast("Push failed: " + errMsg(err), { duration: 6000 });
  }
  // The old `setTimeout(refreshInstances, 1000)` could not observe anything: the
  // server serves GET /api/instances from its tick snapshot for up to 10s. Read
  // the one session fresh instead — and the server's push watcher republishes the
  // moment the branch reaches origin, so "Make PR" appears without another poll.
  void freshStage(title);
}

/* --- Fast-track (autopilot) ------------------------------------------------
 * Arm-and-WAIT, deliberately: a press records the target rung and the server
 * driver takes each step as the session becomes ready for it. That is what lets
 * you arm a session the moment you kick it off instead of babysitting it — and
 * it is what makes this button and the intake depth option the same mechanism.
 * The cost is that the effect is not instant, so the pane must show the armed
 * chip immediately or the press reads as broken. */

/** The rung the ⏩ button will stop at, for LABELLING only.
 *
 * Display-only on purpose. There used to be a sticky `localStorage` value that
 * outranked the server setting — and the commit dialog wrote it on every dropdown
 * change — so browsing that dropdown once pinned every ⏩ button on the machine
 * forever and Settings appeared to do nothing. There is now exactly one
 * authority: the server. It resolves the depth whenever a request omits one, and
 * reports the resolved value on /api/config purely so the UI can name it. */
export function resolveDepth(): string {
  const cfg = queryClient.getQueryData<Config>(["config"]);
  return normalizeDepth(cfg?.fasttrack_depth) || "pr";
}

/** Arm the chain. `message` is only needed when there is uncommitted work and
 * nothing is on disk to reuse — the same rule POST /commit applies. */
export async function startFastTrack(
  title: string,
  depth?: string,
  message?: string,
  base?: string
) {
  if (!title || !requireGit()) return;
  // An explicit pick (the commit dialog, an intake row) is honoured; otherwise the
  // body carries NO depth and the server applies the configured rung. That is what
  // makes changing Settings take effect on every open window immediately.
  const chosen = normalizeDepth(depth || "");
  const d = chosen || resolveDepth();
  // One up-front confirm for the irreversible rung, before anything is armed.
  if (d === "merge") {
    const where = base ? " into " + base : "";
    if (!confirm("Fast-track will commit, push, open a PR and MERGE it" + where + ".\nContinue?"))
      return;
  }
  // Flip the toggle NOW. The button's appearance is derived from the cached
  // `autopilot` block, so waiting for the round trip made a local, instant action
  // feel like a laggy one. The server's answer settles it a moment later; a
  // failure rolls it back and says so.
  const before = instances().find((i) => i.title === title)?.autopilot ?? null;
  patchInstance(title, {
    autopilot: {
      depth: d,
      state: "running",
      step: "",
      reason: "",
      source: "session",
      item: "",
    },
  });
  try {
    const r = await instApi<{ autopilot?: AutopilotRun | null }>(title, "/fast-track", {
      json: {
        ...(chosen ? { depth: chosen } : {}),
        ...(message ? { message } : {}),
        ...(base ? { base } : {}),
      },
    });
    // Reconcile with the authoritative record the route already returns — no
    // follow-up read needed, and arming changes no git state, so the old
    // freshStage() call here was a full row recompute for nothing.
    if (r?.autopilot) patchInstance(title, { autopilot: r.autopilot });
    toast("Fast-tracking to " + depthLabel(d), { duration: 4000 });
  } catch (err) {
    patchInstance(title, { autopilot: before });
    toast("Fast-track failed: " + errMsg(err), { duration: 6000 });
  }
}

/** Disarm. Anything already typed into the shell keeps running — this only stops
 * the driver from taking the NEXT step, which is all it controls. */
export async function stopFastTrack(title: string) {
  if (!title) return;
  const before = instances().find((i) => i.title === title)?.autopilot ?? null;
  patchInstance(title, { autopilot: null }); // toggle off immediately
  try {
    await instApi(title, "/fast-track", { method: "DELETE" });
    toast("Fast-track stopped");
  } catch (err) {
    patchInstance(title, { autopilot: before });
    toast("Could not stop fast-track: " + errMsg(err), { duration: 6000 });
  }
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
    toast("Could not reset this window: " + errMsg(err), { duration: 6000 });
  }
}

export async function ideSession(title: string, quiet = false) {
  if (!title) return;
  try {
    await instApi(title, "/ide", { method: "POST" });
  } catch (err) {
    if (quiet) toast(ideName() + ": " + errMsg(err), { duration: 7000 });
    else alert(ideName() + ": " + errMsg(err));
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
export async function submitMakePr(title: string, base: string) {
  if (!title || !requireGit()) return;
  markStep(title, "pr");
  try {
    const r = await instApi<MakePrResult>(title, "/make-pr", {
      json: base ? { base } : {},
    });
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
    toast("Make PR failed: " + errMsg(err), { duration: 6000 });
  }
  await freshStage(title);
}

/** Merge the branch's PR. Same shape as make-pr: `ok: false` + `pr_url` means
 * "merge it yourself on GitHub", which is a link, not a failure. */
export async function mergeSession(title: string) {
  if (!title || !requireGit()) return;
  if (!confirm("Merge this branch's PR into staging?")) return;
  markStep(title, "merge");
  try {
    const r = await instApi<MergePrResult>(title, "/merge-pr", { method: "POST" });
    if (r && r.ok === false) {
      const msg = r.message || PR_REMEDY;
      if (r.pr_url) offerUrl(r.pr_url, "Opened the PR on GitHub to merge there — " + msg);
      else toast(msg, { duration: 9000 });
    }
  } catch (err) {
    clearStep(title);
    toast("Merge failed: " + errMsg(err), { duration: 6000 });
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

export async function pauseSession(title: string) {
  if (!title) return;
  try {
    await instApi(title, "/pause", { method: "POST" });
  } catch (err) {
    toast("Pause failed: " + errMsg(err), { duration: 5000 });
  }
  await refreshInstances();
}

export async function resumeSession(title: string) {
  if (!title) return;
  try {
    await instApi(title, "/resume", { method: "POST" });
  } catch (err) {
    toast("Resume failed: " + errMsg(err), { duration: 5000 });
  }
  await refreshInstances();
}
