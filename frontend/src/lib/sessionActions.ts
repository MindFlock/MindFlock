/** Session actions shared by the sidebar ⋯ menu, keyboard shortcuts, the
 * command palette, and pane buttons (port of app.js section 10).
 *
 * Dialog-opening actions (commit/rename/device) go through the UI store; the
 * dialog components own their submit logic. */

import { api, instApi } from "../api/client";
import type { Config, Instance } from "../api/types";
import { computeVisible } from "../components/grid/layout";
import { queryClient, refreshInstances } from "../state/queries";
import { useUi } from "../state/store";
import { toast } from "./toast";
import { errMsg } from "./format";
import { markLoopReset } from "./stage";
import { focusTerm, releaseTerms } from "./terminals";

function caps() {
  return queryClient.getQueryData<Config>(["config"])?.caps ?? {
    git: true,
    tailscale: true,
    ticketing: true,
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
  const visible = computeVisible(instances(), {
    hidden: ui.hidden,
    viewMode: ui.viewMode,
    mru: ui.mru,
    order: ui.order,
  }).map((i) => i.title);
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

export function requireGit(): boolean {
  if (caps().git) return true;
  toast("git is not installed — install git to use diffs, commits and PRs");
  return false;
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
      toast("Can’t reopen “" + (ent.title || "session") + "” — its worktree is gone", {
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
      toast("Reopened " + (ent.title || "session"));
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
}

/** Copy a window: a second session sharing this one's worktree. */
export async function copySession(title: string) {
  if (!title) return;
  const guess = addPendingSession(title + "-copy");
  try {
    const inst = await instApi<Instance>(title, "/copy", { method: "POST" });
    await refreshInstances();
    if (inst?.title) selectSession(inst.title);
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
  try {
    await instApi(title, "/push-branch", { json: force ? { force: true } : {} });
  } catch (err) {
    // O3 soft gate: checks haven't passed — offer an explicit override.
    if ((err as Error).message === "checks haven't passed for this commit") {
      if (
        confirm("Checks haven't passed for this commit (see the ✗ checks chip).\nPush anyway?")
      )
        return pushSession(title, true);
      return;
    }
    toast("Push failed: " + errMsg(err), { duration: 6000 });
  }
  setTimeout(refreshInstances, 1000);
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
 * the Make-PR dialog once the user picks a branch. */
export async function submitMakePr(title: string, base: string) {
  if (!title || !requireGit()) return;
  try {
    const r = await instApi<{ url?: string }>(title, "/make-pr", {
      json: base ? { base } : {},
    });
    if (r?.url) window.open(r.url, "_blank");
    // PR is open — restart the guided cycle: pin the pill back to idle so the
    // button reads "Commit…" again and the commit→push→PR loop can repeat in
    // this session (the pin self-clears once real new work moves the stage).
    markLoopReset(title);
  } catch (err) {
    alert("Make PR failed: " + errMsg(err));
  }
  await refreshInstances();
}

export async function mergeSession(title: string) {
  if (!title || !requireGit()) return;
  if (!confirm("Merge this branch's PR into staging?")) return;
  try {
    await instApi(title, "/merge-pr", { method: "POST" });
  } catch (err) {
    alert("Merge failed: " + errMsg(err));
  }
  await refreshInstances();
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
