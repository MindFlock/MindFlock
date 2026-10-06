/** Split and one-for-all groups, the pure half: what the lead's Thread tab,
 * its rail row and the bell say about a group whose lines merge back into
 * ONE branch and ship as ONE PR (SPEC §3.4, §7.C.4).
 *
 * The server does every step — it starts the workers, fences each to its
 * paths, merges each one back into the lead's branch as it finishes, hands a
 * conflict to the lead, runs the check on the merged branch and opens the PR
 * when you say so. Nothing here pastes a prompt into an agent: the Thread
 * only shows where that is and offers the one click that is yours (approve
 * the plan, open the PR).
 *
 * No DOM, no store, so every sentence is unit-tested in node. */

import type { PlanPiece, RunDTO, RunTask, SplitMode } from "../api/types";
import { LANE_HEAD } from "./agentMessages";

/** A group whose lines merge back into one branch (a split, or "one for all"). */
export function isTogether(run: Pick<RunDTO, "policy"> & Partial<Pick<RunDTO, "split">>): boolean {
  return !!run.split || run.policy?.grouping === "together";
}

/** The first line of a prompt, trimmed for a one-line row. */
export function firstLine(text: string | null | undefined, max = 120): string {
  const s = String(text || "")
    .trim()
    .split(/\r?\n/)[0]
    .trim();
  return s.length > max ? s.slice(0, max - 1).trimEnd() + "…" : s;
}

/** "24 tests" from a worker's `Tests:` line ("pytest tests/auth — 24 passed"),
 * or "" when it names no count. */
export function testsLabel(tests: string | number | null | undefined): string {
  if (typeof tests === "number") return tests > 0 ? tests + (tests === 1 ? " test" : " tests") : "";
  const s = String(tests || "");
  const m = s.match(/(\d+)\s+(?:tests?\s+)?passed/i) || s.match(/(\d+)\s+tests?\b/i);
  if (!m) return "";
  const n = Number(m[1]);
  return n + (n === 1 ? " test" : " tests");
}

/** The tasks that are the group's lines (pieces of a split, or the lines of a
 * one-for-all batch), in the order the server keeps them. */
export function memberTasks(run: Pick<RunDTO, "tasks">): RunTask[] {
  return (run.tasks || []).filter((t) => t.state !== "cancelled" || !!t.title);
}

/** A piece's short name and what it is for: the plan's own words when the
 * plan has the piece ("tokens — rotate refresh tokens on privilege change"),
 * else the task's title and line. */
export function pieceLabel(t: RunTask, run: Pick<RunDTO, "plan" | "tasks">): { name: string; what: string } {
  const pieces = run.plan?.pieces || [];
  const ix = (run.tasks || []).filter((x) => x.kind === "piece").indexOf(t);
  const p = t.kind === "piece" && ix >= 0 ? pieces[ix] : undefined;
  if (p) return { name: p.title, what: firstLine(p.prompt, 90) };
  const what = firstLine(t.text, 90);
  return {
    name: t.title || what || t.id,
    what: t.title && what !== t.title ? what : "",
  };
}

/** "only here: a/b*, c/d" — a piece's fence, for its row's code chip. */
export function pathsText(paths: readonly string[] | null | undefined): string {
  return (paths || []).filter(Boolean).join(", ");
}

export interface PieceStatus {
  /** The bold word on the right ("merged back ✓"). */
  word: string;
  /** The dot and word colour: ok / needs / bad / work / idle. */
  cls: "ok" | "needs" | "bad" | "work" | "idle";
  /** A quieter follow-on ("· 24 tests", "· auth-cleanup-lead is resolving x.py"). */
  detail: string;
}

/** A split whose pieces run in the lead's own folder (no merge). */
export function isSameFolder(run: Pick<RunDTO, "mode"> | null | undefined): boolean {
  return run?.mode === "same_folder";
}

/** Where one piece is, from its task. `mode` = the run's: a same-folder
 * piece is committed by MindFlock, never merged back. */
export function pieceStatus(t: RunTask, leadName: string, mode?: SplitMode): PieceStatus {
  const tests = testsLabel(t.tests);
  const files = (t.conflict?.files || []).join(", ");
  if (mode === "same_folder") {
    if (t.state === "integrated") return { word: "committed ✓", cls: "ok", detail: tests };
    if (t.state === "integrating") return { word: "committing…", cls: "work", detail: "" };
  }
  switch (t.state) {
    case "integrated":
      return { word: "merged back ✓", cls: "ok", detail: tests };
    case "integrating":
      if (t.reason === "conflict" || t.conflict)
        return {
          word: "conflict",
          cls: "needs",
          detail: leadName + " is resolving" + (files ? " " + files : " it"),
        };
      return { word: "merging…", cls: "work", detail: "" };
    case "needs_you":
      return {
        word: t.reason === "conflict" ? "conflict — needs you" : "needs you",
        cls: "needs",
        detail: t.detail || "",
      };
    case "failed":
      return { word: "failed", cls: "bad", detail: t.detail || "" };
    case "shipping":
      return { word: "committing", cls: "work", detail: "" };
    case "working":
      return { word: "working", cls: "work", detail: "" };
    case "starting":
      return { word: "starting", cls: "idle", detail: "" };
    case "queued":
      return { word: "queued", cls: "idle", detail: "" };
    case "skipped":
    case "cancelled":
      return { word: "left out", cls: "idle", detail: t.detail || "" };
    case "shipped":
      return { word: "done", cls: "ok", detail: tests };
    default:
      return { word: t.state || "—", cls: "idle", detail: "" };
  }
}

/** A run of text in the Thread's sub-line: `b` bold, `cls` coloured. */
export interface SubPart {
  text: string;
  b?: boolean;
  cls?: "ok" | "bad" | "needs";
}

function plural(n: number, one: string, many = one + "s"): string {
  return n + " " + (n === 1 ? one : many);
}

/** The lead's Thread sub-line, e.g. "Split into **3 pieces with separate
 * paths** · all 3 merged back into **run/auth-cleanup** · 212 tests pass
 * after the last merge". */
export function leadSubline(run: RunDTO, leadName: string): SubPart[] {
  const split = !!run.split;
  const tasks = memberTasks(run);
  const n = split && run.plan && run.state === "plan_ready" ? run.plan.pieces.length : tasks.length;
  const branch = run.lead?.branch || run.release?.branch || "";
  if (run.state === "planning")
    return [
      {
        text: run.optional
          ? "Waiting for " +
            leadName +
            " to decide whether to split — it reads the code first; a plan shows here, or it just does the task."
          : "Waiting for " +
            leadName +
            " to propose the pieces — it reads the code first, then MindFlock shows the plan here.",
      },
    ];
  if (run.state === "plan_ready")
    return [
      { text: leadName + " proposed " },
      { text: plural(n, "piece") + " with separate paths", b: true },
      { text: " — approve them and MindFlock starts the workers." },
    ];
  const parts: SubPart[] = split
    ? [{ text: "Split into " }, { text: plural(n, "piece") + " with separate paths", b: true }]
    : [{ text: plural(n, "line") + ", one PR", b: true }];
  const merged = tasks.filter((t) => t.state === "integrated").length;
  const live = tasks.filter((t) => !["cancelled", "skipped", "failed"].includes(t.state)).length;
  if (isSameFolder(run)) {
    // In the lead's own folder: MindFlock commits each piece's paths.
    parts.push({ text: " in " + leadName + "'s folder · " });
    if (merged) {
      parts.push({
        text: merged === live && merged > 1 ? "all " + merged + " committed" : merged + " of " + live + " committed",
        cls: "ok",
      });
      parts.push({ text: " on " });
      parts.push({ text: branch || "the lead's branch", b: true });
    } else {
      parts.push({ text: "each piece is committed on " });
      parts.push({ text: branch || "the lead's branch", b: true });
      parts.push({ text: " as it finishes" });
    }
  } else if (merged) {
    parts.push({ text: " · " });
    parts.push({
      text: merged === live && merged > 1 ? "all " + merged + " merged back" : merged + " of " + live + " merged back",
      cls: "ok",
    });
    if (branch) {
      parts.push({ text: " into " });
      parts.push({ text: branch, b: true });
    }
  } else if (live) {
    parts.push({ text: " · merging back into " });
    parts.push({ text: branch || "the lead's branch", b: true });
    parts.push({ text: " as each finishes" });
  }
  const conflicts = tasks.filter((t) => t.state === "integrating" && (t.reason === "conflict" || t.conflict)).length;
  if (conflicts)
    parts.push({
      text: " · " + plural(conflicts, "conflict") + " with " + leadName,
      cls: "needs",
    });
  const c = run.check;
  if (c && c.state === "ok") {
    const tests = testsLabel(c.tests ?? null);
    parts.push({ text: " · " });
    parts.push({
      text: tests ? tests + " pass" : "the check passed",
      cls: "ok",
    });
    parts.push({ text: " after the last merge" });
  } else if (c && c.state === "running") parts.push({ text: " · running the check on the merged branch" });
  else if (c && c.state === "failed")
    parts.push({
      text: " · the check failed on the merged branch",
      cls: "bad",
    });
  return parts;
}

// --- The plan card (plan_ready) ---------------------------------------------------

export interface ModeChoice {
  mode: "worktrees" | "same_folder";
  label: string;
  /** The one short line that says what it trades. */
  hint: string;
  /** Why it can't be picked as things stand ("" = it can). */
  blocked: string;
}

/** The plan card's "where do the pieces run" choice. A lead that works
 * directly in its folder, or sits on its trunk, is never merged into:
 * separate worktrees get a lead of their own (`<lead>-split`, from its last
 * commit), and its folder needs a branch of its own first. */
export function modeChoices(
  run: Pick<RunDTO, "lead">,
  leadName: string
): ModeChoice[] {
  const lead = run.lead || { title: "" };
  const branch = lead.branch || "";
  const own = !lead.in_place && !lead.trunk;
  return [
    {
      mode: "worktrees",
      label: "In separate worktrees (merge back) — default",
      hint: own
        ? "Each piece in its own worktree, merged back into " + (branch || leadName + "'s branch") + "; conflicts go to " + leadName + "."
        : "MindFlock starts " +
          leadName +
          "-split from " +
          leadName +
          "'s last commit and merges the pieces there — " +
          leadName +
          " itself is left as it is.",
      blocked: "",
    },
    {
      mode: "same_folder",
      label: "In this folder (no merge)",
      hint:
        "Each piece runs in " +
        leadName +
        "'s folder; MindFlock commits each piece's paths as it finishes — nothing to merge, no per-piece undo.",
      blocked: lead.trunk ? leadName + " is on " + (branch || "its trunk") + " — the pieces would commit onto it" : "",
    },
  ];
}

/** The toast once the workers start. */
export function startedText(n: number, mode: "worktrees" | "same_folder", leadName: string): string {
  const w = startWorkersLabel(n).replace(/^Start/, "Starting");
  return mode === "same_folder"
    ? w + " in " + leadName + "'s folder — each fenced to its paths"
    : w + " — each fenced to its paths";
}

export interface PlanRow {
  title: string;
  prompt: string;
  paths: string[];
  pathsText: string;
}

export function planRows(pieces: readonly PlanPiece[] | null | undefined): PlanRow[] {
  return (pieces || []).map((p) => ({
    title: String(p.title || ""),
    prompt: String(p.prompt || ""),
    paths: (p.paths || []).map(String),
    pathsText: pathsText(p.paths),
  }));
}

/** "Start 3 workers". */
export function startWorkersLabel(n: number): string {
  return "Start " + plural(n, "worker");
}

/** The paths field as typed (commas, spaces or new lines between globs). */
export function parsePaths(text: string): string[] {
  return String(text || "")
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** One edited piece put back into the plan (the rest unchanged). */
export function editPlan(
  pieces: readonly PlanPiece[],
  i: number,
  next: { title: string; prompt: string; paths: string },
): PlanPiece[] {
  return pieces.map((p, j) =>
    j === i
      ? {
          title: next.title.trim(),
          prompt: next.prompt.trim(),
          paths: parsePaths(next.paths),
        }
      : { ...p, paths: [...p.paths] },
  );
}

/** A 422's problems as lines ("scopes: overlaps sessions on x.py"). */
export function planProblems(body: unknown): string[] {
  const b = (body || {}) as {
    problems?: Array<{ piece?: string; error?: string }>;
    error?: string;
  };
  const out = (b.problems || []).map((p) => (p.piece ? p.piece + ": " : "") + String(p.error || "")).filter(Boolean);
  if (!out.length && b.error) out.push(String(b.error));
  return out;
}

// --- The ship card (release_ready) -------------------------------------------------

export interface ReleaseCard {
  title: string;
  /** "main ← run/auth-cleanup" */
  into: string;
  /** "14 files +412 −188" ("" when unknown) */
  stat: string;
  commits: string;
  body: string;
}

export function releaseCard(run: RunDTO): ReleaseCard {
  const r = run.release || { state: "none" };
  const base = r.base || "the base branch";
  const branch = r.branch || run.lead?.branch || "";
  const files = Number(r.files) || 0;
  const stat = files ? plural(files, "file") + " +" + (Number(r.add) || 0) + " −" + (Number(r.del) || 0) : "";
  const fixes = Number(r.conflict_fixes) || 0;
  const split = !!run.split;
  return {
    title: r.title || run.name,
    into: base + " ← " + (branch || "its branch"),
    stat,
    commits:
      (isSameFolder(run)
        ? "one per piece, committed by MindFlock with only its paths"
        : split
          ? "one per piece, kept as written"
          : "one per line, kept as written") +
      (fixes ? " + " + plural(fixes, "conflict fix", "conflict fixes") + " by the lead" : ""),
    body: split
      ? "a section per piece: what changed, the tests it ran"
      : "a section per line: what changed, the tests it ran",
  };
}

/** The check row of the ship card — always said, including "none". */
export function checkLine(run: Pick<RunDTO, "check"> & Partial<Pick<RunDTO, "mode">>): string {
  const c = run.check;
  const cmd = c?.command ? "`" + c.command + "`" : "the check";
  switch (c?.state) {
    case "none":
      return "no check configured — add a check_command to .mindflock.toml to run one";
    case "ok":
      return cmd + " passed" + (c.tests ? " (" + c.tests + " tests)" : "");
    case "failed":
      return cmd + " failed" + (c.summary ? ": " + c.summary : "");
    case "running":
      return cmd + " is running on the merged branch…";
    case "fixing":
      return cmd + " failed — the lead is fixing it";
    case "pending":
      return isSameFolder(run) ? "runs once every piece is committed" : "runs once every piece is merged back";
    default:
      return "";
  }
}

/** The release buttons a ready group offers, labelled from the group's OWN
 * lane — the primary is the lane the user chose. "Open the PR" never merges
 * (the server caps it at a PR); only the explicit "merge when checks pass"
 * does; a push group pushes its branch and opens nothing. */
export function releaseChoices(
  lane: string | null | undefined,
  localOrigin?: string | null
): Array<{ merge: boolean; label: string; primary: boolean; title: string }> {
  // The lead's origin is a folder on this machine: a push is all a release
  // can do there (the server never attempts the PR), so that is all it offers.
  if (localOrigin)
    return [
      {
        merge: false,
        label: "Push to the local folder",
        primary: true,
        title: "Push the group's branch to " + localOrigin + " — a folder on this machine, not GitHub: no PR can be opened",
      },
    ];
  if (lane === "push")
    return [{ merge: false, label: "Push the branch", primary: true, title: "Push the group's branch — no PR is opened" }];
  const pr = {
    merge: false,
    label: lane === "merge" ? "Open the PR only" : "Open the PR",
    primary: lane !== "merge",
    title: "Open the group's one PR — it is not merged",
  };
  const merge = {
    merge: true,
    label: lane === "merge" ? "Open the PR, merge when checks pass" : "Open it, merge when checks pass",
    primary: lane === "merge",
    title: "Open the group's one PR and merge it once its checks pass",
  };
  return lane === "merge" ? [merge, pr] : [pr, merge];
}

/** "fast-track: → PR, asks first" — the lead row's fast-track. */
export function laneNote(lane: { target?: string; ask_first?: boolean } | null | undefined): string {
  const t = String(lane?.target || "");
  if (!t) return "";
  const head = t === "leave" ? "off" : LANE_HEAD[t] || "→ " + t;
  return "fast-track: " + head + (lane?.ask_first ? ", asks first" : "");
}

/** What the release half says once it is past ready (null while there is
 * nothing to say). */
export function releaseOutcome(run: RunDTO): {
  text: string;
  cls: "ok" | "bad" | "work" | "idle";
  url: string;
  link: string;
} | null {
  const r = run.release;
  const lane = run.policy?.lane || "";
  const local = r?.local_origin || "";
  if (run.state === "releasing" || r?.state === "releasing")
    return {
      text: local ? "Pushing the branch to " + local + "…" : lane === "push" ? "Pushing the branch…" : "Opening the PR…",
      cls: "work",
      url: "",
      link: "",
    };
  if (r?.state === "done" && r.pr_url) {
    const m = r.pr_url.match(/\/pull\/(\d+)/);
    return {
      text: "✓ " + (m ? "PR #" + m[1] : "PR") + " opened",
      cls: "ok",
      url: r.pr_url,
      link: "Open the PR ↗",
    };
  }
  // Pushed into a folder on this machine: say where, and that no PR exists —
  // the title and body below are the hand-off for opening it on the forge.
  if ((r?.state === "handoff" || r?.state === "done") && local)
    return {
      text:
        "Pushed " +
        (r.branch || run.lead?.branch || "the branch") +
        " to " +
        local +
        " — a folder on this machine, not GitHub, so no PR was opened. Push the branch to your forge and open the PR there" +
        (r.title ? " (copy its title and body below)" : ""),
      cls: "idle",
      url: "",
      link: "",
    };
  if (r?.state === "handoff")
    return {
      text: r.compare_url
        ? "Pushed — MindFlock couldn't open the PR here (no gh or token)"
        : "Pushed — MindFlock couldn't open the PR here: copy its title and body below into a PR on your host",
      cls: "idle",
      url: r.compare_url || "",
      link: r.compare_url ? "Open the compare page ↗" : "",
    };
  if (r?.state === "failed")
    return {
      text: "The release stopped: " + (r.detail || "see the lead's pane"),
      cls: "bad",
      url: "",
      link: "",
    };
  if (r?.state === "done")
    return {
      text: lane === "push" ? "✓ pushed" : "✓ released",
      cls: "ok",
      url: "",
      link: "",
    };
  if (run.state === "done" && (lane === "commit" || lane === "leave"))
    return {
      text:
        "All merged into " +
        (run.lead?.branch || "the lead's branch") +
        " — nothing pushed (this group is fast-tracked to " +
        (lane === "commit" ? "Commit" : "Off") +
        ")",
      cls: "ok",
      url: "",
      link: "",
    };
  return null;
}

// --- The rail -----------------------------------------------------------------------

/** The extra chips a group row keeps. A merged-back piece says "merged" and
 * nothing more — its passing check and its green-zone shield are history
 * once its work is in the lead's branch — and a lead asking for your click
 * (`→ PR?`, `plan?`) drops its check chip so its NAME keeps the room. Only
 * a chip that still needs you (a failed check, a breach, a guard not
 * holding) survives. */
export function railExtraChips<C extends { cls: string } | null, Z extends { cls: string } | null>(
  check: C,
  rz: Z,
  o: { integrated: boolean; leadAsks: boolean }
): { check: C | null; rz: Z | null } {
  if (!o.integrated && !o.leadAsks) return { check, rz };
  const loud = (c: { cls: string } | null) => !!c && /(interrupt|breach|warn)/.test(c.cls);
  return { check: loud(check) ? check : null, rz: loud(rz) ? rz : null };
}

/** The lead row's chip: `→ PR?` (filled, clickable → its Thread) once one PR
 * is ready to open, `plan?` while its plan waits for you. null otherwise. */
export function leadChip(
  run: Pick<RunDTO, "state" | "policy" | "name"> | null | undefined,
): { label: string; title: string } | null {
  if (!run) return null;
  if (run.state === "release_ready") {
    const lane = run.policy?.lane || "pr";
    const label = lane === "push" ? "→ push?" : lane === "merge" ? "→ merge?" : "→ PR?";
    return {
      label,
      title: "Everything merged back and checked — open its Thread to release the one PR",
    };
  }
  if (run.state === "plan_ready")
    return {
      label: "plan?",
      title: "The lead proposed the pieces — open its Thread to approve them",
    };
  return null;
}

/** The lead row's status line: how the group is going ("3 of 3 merged back"). */
export function leadLine(
  run: RunDTO | null | undefined
): { text: string; cls: "ok" | "needs" | "bad" | ""; url?: string } | null {
  if (!run) return null;
  const tasks = memberTasks(run);
  const merged = tasks.filter((t) => t.state === "integrated").length;
  const live = tasks.filter((t) => !["cancelled", "skipped", "failed"].includes(t.state)).length;
  switch (run.state) {
    case "planning":
      return { text: run.optional ? "deciding whether to split…" : "proposing the pieces…", cls: "" };
    case "plan_ready":
      return { text: "plan ready — approve it", cls: "needs" };
    case "checking":
      return run.check?.state === "failed"
        ? { text: "the check failed", cls: "bad" }
        : { text: "running the check", cls: "" };
    case "releasing":
      return { text: "opening the PR", cls: "" };
    case "cancelled":
      return { text: "cancelled", cls: "" };
  }
  if (run.release?.state === "done" && run.release.pr_url) {
    const m = run.release.pr_url.match(/\/pull\/(\d+)/);
    return { text: "✓ " + (m ? "PR #" + m[1] : "PR"), cls: "ok", url: run.release.pr_url };
  }
  // The release pushed but couldn't open the PR here (no gh, no token): that
  // is how it is meant to end without them — the PR is one click away, never
  // a red "fast-track stopped".
  if (run.release?.local_origin && (run.release.state === "handoff" || run.release.state === "done"))
    return { text: "⇡ pushed to a local folder — no PR", cls: "" };
  if (run.release?.state === "handoff")
    return { text: "⇡ pushed — open the PR", cls: "", url: run.release.compare_url || undefined };
  if (tasks.some((t) => t.state === "integrating" && (t.reason === "conflict" || t.conflict)))
    return { text: "resolving a conflict", cls: "needs" };
  if (!live) return null;
  return {
    text: merged + " of " + live + (isSameFolder(run) ? " committed" : " merged back"),
    cls: merged === live ? "ok" : "",
  };
}
