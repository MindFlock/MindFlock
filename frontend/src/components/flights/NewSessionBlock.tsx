/** The Sky's new-session block: a block on the sky that opens where the
 * owner double-clicked empty sky (or dropped the shelf's Session chip) — pick
 * a repository, say what to do, optionally a saved stop to fly, and Fly
 * (docs/flights-intake-lane.md, docs/flights-stable.md). Its bar (the bird,
 * "New session") drags it about like a line's bar (`onMove`, plane px; the
 * sky's `zoom` turns pointer px into plane px); the session it makes is
 * pinned where the block sat.
 *
 *  - Repository: the New dialog's suggestions (`/api/repos/suggest` — recent
 *    first) as chips, plus a path field with Browse… (the shared folder
 *    picker window).
 *  - Prompt: the New dialog's prompt box; Enter flies, Shift+Enter is a new
 *    line.
 *  - Stops: "No stops" or one of the saved stops, built-ins first. Not shown
 *    for a block dropped on a grounded line (`attach`): its session flies
 *    THOSE stops (`POST /api/flights/{id}/attach {title}`).
 *  - Agent: chips when more than one CLI is installed; the default preselected.
 *
 * The create body is the New dialog's own (`newSessionSubmit(…).buildBody`, so
 * the prompt, the worktree rules and the per-agent launch flags are exactly
 * what the dialog would send); a picked saved stop then takes off on the new
 * session (`startFlightOn`). Errors stay in the card — Electron has no
 * alert(). Escape cancels.
 *
 * Fast: the moment Fly is pressed the optimistic `pending_create` row is in
 * the sessions (`addPendingSession`) and `onPending(guess)` tells the sky,
 * which pins that row's line where the block sat and hides the block; a
 * refusal brings the block back with the error (`onFailed`). */

import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../../api/client";
import type { Instance } from "../../api/types";
import { refreshInstances, useAgentChoices, useSettingsDoc } from "../../state/queries";
import { rememberNewSessionChoice } from "../../state/store";
import { addPendingSession, clearStaleAlias, failPendingSession } from "../../lib/sessionActions";
import { newSessionSubmit, type NewSessionSubmitCtx } from "../../lib/newSessionSubmit";
import { attachGrounded, startFlightOn } from "../../lib/flightsApi";
import { windowName } from "../../lib/windowName";
import { errMsg } from "../../lib/format";
import { isNameQuery, titleFromPrompt, type RepoSuggestion } from "../dialogs/newSession";
import { PathField } from "../PathField";
import type { FlightPlan } from "../../lib/flights";
import { ChipRow } from "./controls";
import { Bird } from "./Bird";
import "./FlywayCanvas.css";
import "./NewSessionBlock.css";

export interface NewSessionBlockProps {
  x: number;
  y: number;
  plans: FlightPlan[];
  /** The sky's zoom, so a drag of the bar follows the pointer (default 1). */
  zoom?: number;
  /** The bar was dragged: the block's new top-left (plane px). Without it the
   * block stays put (the phone's list). */
  onMove?(at: { x: number; y: number }): void;
  onCreated(title: string): void;
  onCancel(): void;
  /** Made for a grounded line's stops: they take off on the new session. */
  attach?: { id: string; name: string };
  /** Fly pressed: the optimistic row's (guessed) title is in the sessions. */
  onPending?(guess: string): void;
  /** The create was refused: the block shows again, with why. */
  onFailed?(): void;
  /** Drawn but not shown (its session's line already is). */
  hidden?: boolean;
}

/** How many suggested repositories show as chips. */
const REPO_CHIPS = 6;

/** The saved-stop chips: "No stops" first, then built-ins, then the owner's. */
export function savedStopChoices(plans: readonly Pick<FlightPlan, "id" | "name" | "builtin">[]): Array<{ value: string; label: string }> {
  const ordered = [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];
  return [{ value: "", label: "No stops" }, ...ordered.map((p) => ({ value: p.id, label: p.name }))];
}

/** The repository chips: the suggestions, recent first, one per path. */
export function repoChoices(suggestions: readonly RepoSuggestion[], max = REPO_CHIPS): Array<{ value: string; label: string; title: string }> {
  const rank = (s: RepoSuggestion) => (s.source === "recent" ? 0 : s.source === "cwd" ? 1 : 2);
  const seen = new Set<string>();
  const out: Array<{ value: string; label: string; title: string }> = [];
  for (const s of [...suggestions].sort((a, b) => rank(a) - rank(b))) {
    if (!s.path || seen.has(s.path)) continue;
    seen.add(s.path);
    out.push({ value: s.path, label: s.name || s.path.split("/").filter(Boolean).pop() || s.path, title: s.path });
    if (out.length >= max) break;
  }
  return out;
}

/** Why Fly is greyed out ("" = it can fly). */
export function flyBlocked(repo: string): string {
  if (!repo.trim()) return "Pick a repository first";
  if (isNameQuery(repo)) return "Type a full path starting with / or ~ (Browse… fills one in)";
  return "";
}

/** The keys a card or field handler reads. */
interface KeyLike {
  key: string;
  shiftKey?: boolean;
  nativeEvent?: { isComposing?: boolean };
  preventDefault(): void;
  stopPropagation(): void;
}

/** Escape anywhere in the card cancels it — and stops there, so the sky (or
 * the dialog under it) doesn't also take the key. */
export function cardKey(e: KeyLike, onCancel: () => void): void {
  if (e.key !== "Escape") return;
  e.preventDefault();
  e.stopPropagation();
  onCancel();
}

/** The prompt box: Enter flies, Shift+Enter is a new line (and an IME's
 * Enter only finishes its word). */
export function promptKey(e: KeyLike, fly: () => void): void {
  if (e.key !== "Enter" || e.shiftKey || e.nativeEvent?.isComposing) return;
  e.preventDefault();
  fly();
}

const noop = () => {};

/** The New dialog's create path, with the form's choices this card makes and
 * the dialog's own defaults for everything it doesn't show. */
function bodyFor(opts: { repo: string; prompt: string; program: string; launchArgs: string }): Record<string, unknown> {
  const ref = <T,>(v: T) => ({ current: v });
  const ctx: NewSessionSubmitCtx = {
    title: "",
    repoPath: opts.repo,
    prompt: opts.prompt,
    program: opts.program,
    planFirst: false,
    planOk: false,
    launchArgs: opts.launchArgs,
    profileId: "",
    profileModel: "",
    strategy: "worktree",
    inPlace: false,
    initRepo: false,
    provision: false,
    baseBranch: "",
    baseIsRemote: false,
    continueOn: "",
    activeTemplate: "",
    maxPieces: 0,
    splitLimit: 0,
    runMode: false,
    splitOn: false,
    laneNeedsWorktree: false,
    newFolderBlocked: "",
    provisionBlocked: "",
    draft: null as unknown as NewSessionSubmitCtx["draft"],
    laneAtPress: ref({ lane: "leave", askFirst: false }) as NewSessionSubmitCtx["laneAtPress"],
    deviceRef: ref(""),
    failedReopen: ref(false),
    submitArmAt: ref(0),
    setError: noop,
    setPlanError: noop,
    setPromptOpen: noop,
    setPage: noop,
    planFolderDo: noop,
    closeDialog: noop,
  };
  return newSessionSubmit(ctx).buildBody({
    title: "",
    repoPath: opts.repo,
    prompt: opts.prompt,
    inPlace: false,
    initRepo: false,
    provisioned: false,
  });
}

export function NewSessionBlock({ x, y, plans, zoom = 1, onMove, onCreated, onCancel, attach, onPending, onFailed, hidden }: NewSessionBlockProps) {
  const [repo, setRepo] = useState("");
  const [prompt, setPrompt] = useState("");
  const [plan, setPlan] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<RepoSuggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  /** A session that exists but whose stops refused to take off. */
  const [made, setMade] = useState("");
  const cardRef = useRef<HTMLDivElement | null>(null);

  const agents = useAgentChoices().data;
  const settings = useSettingsDoc(true).data as { coding_cli?: { default_launch_args?: Record<string, unknown> } } | undefined;
  const names = agents?.names || [];
  const program = picked ?? (agents?.fallback || names[0] || "");

  useEffect(() => {
    let live = true;
    api<{ suggestions?: RepoSuggestion[] }>("/api/repos/suggest")
      .then((d) => {
        if (!live) return;
        const sug = d?.suggestions || [];
        setSuggestions(sug);
        // The New dialog opens on its first suggestion; so does the card.
        setRepo((cur) => cur || sug[0]?.path || "");
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    cardRef.current?.querySelector<HTMLTextAreaElement>("textarea")?.focus();
  }, []);

  const repoChips = useMemo(() => repoChoices(suggestions), [suggestions]);
  const blocked = flyBlocked(repo);

  const launchArgs = (() => {
    const raw = settings?.coding_cli?.default_launch_args || {};
    const hit = Object.keys(raw).find((k) => k.toLowerCase() === program.trim().toLowerCase());
    return hit ? String(raw[hit] || "").trim() : "";
  })();

  /** Drag the block by its bar: it follows the pointer, live. */
  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!onMove || e.button !== 0 || (e.target as Element).closest("button")) return;
    e.preventDefault();
    e.stopPropagation();
    const g = { pid: e.pointerId, sx: e.clientX, sy: e.clientY, x, y, z: zoom || 1 };
    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== g.pid) return;
      onMove({ x: Math.round(g.x + (ev.clientX - g.sx) / g.z), y: Math.round(g.y + (ev.clientY - g.sy) / g.z) });
    };
    const up = (ev: PointerEvent) => {
      if (ev.pointerId !== g.pid) return;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  };

  const fly = async () => {
    if (busy || blocked) return;
    if (made) return onCreated(made);
    setBusy(true);
    setError("");
    const body = bodyFor({ repo, prompt, program, launchArgs });
    const guess = addPendingSession(titleFromPrompt(prompt) || repo.split("/").filter(Boolean).pop() || "untitled");
    onPending?.(guess);
    let title = "";
    try {
      const inst = await api<Instance>("/api/instances", { json: body });
      title = inst.title;
      rememberNewSessionChoice(String(body.repo_path || ""), { template: "", program: String(body.program || ""), profile_id: "" });
      clearStaleAlias(title);
      await refreshInstances();
    } catch (err) {
      failPendingSession(guess);
      setError(errMsg(err));
      setBusy(false);
      onFailed?.();
      return;
    }
    // The stops it flies: a grounded line's, or a saved stop picked here.
    const flew = attach ? await attachGrounded(attach.id, { title }) : plan ? await startFlightOn(title, plan) : true;
    if (!flew) {
      setMade(title);
      setError("“" + windowName(title) + "” started, but " + (attach ? "“" + attach.name + "”" : "the stops") + " didn't take off — the error card says why.");
      setBusy(false);
      onFailed?.();
      return;
    }
    setBusy(false);
    onCreated(title);
  };

  return (
    <div
      ref={cardRef}
      className="sky-new sky-new-block fl-form"
      role="dialog"
      aria-label={attach ? "A new session for “" + attach.name + "”" : "New session"}
      hidden={hidden}
      style={{ left: x, top: y }}
      onKeyDown={(e) => cardKey(e, onCancel)}
      onDoubleClick={(e) => e.stopPropagation()}
    >
      <div
        className={"sky-new-head" + (onMove ? " is-movable" : "")}
        title={onMove ? "Drag to move this block — the session starts where it sits" : undefined}
        onPointerDown={onBarDown}
      >
        <Bird size={13} />
        <span className="sky-new-name">{attach ? "New session for “" + attach.name + "”" : "New session"}</span>
        <button type="button" className="sky-new-close" aria-label="Cancel the new session" title="Cancel (Esc)" onClick={onCancel}>
          ×
        </button>
      </div>
      <div className="fl-fld fl-fld-wide">
        <span className="fl-fld-label">Repository</span>
        {repoChips.length > 0 && (
          <ChipRow className="sky-new-repos" label="Repository" value={repo} options={repoChips} onChange={setRepo} />
        )}
        <PathField
          className="sky-new-path"
          value={repo}
          onChange={setRepo}
          pickerTitle="Pick the repository"
          aria-label="Repository folder"
          placeholder="/path/to/repo"
        />
      </div>
      <div className="fl-fld fl-fld-wide">
        <span className="fl-fld-label">Task</span>
        <textarea
          className="fl-prompt sky-new-prompt"
          aria-label="Task"
          rows={3}
          value={prompt}
          placeholder="What should the agent do? Enter flies, Shift+Enter for a new line."
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => promptKey(e, () => void fly())}
        />
      </div>
      {plans.length > 0 && !attach && (
        <div className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Stops</span>
          <ChipRow className="sky-new-stops" label="Stops" value={plan} options={savedStopChoices(plans)} onChange={setPlan} />
        </div>
      )}
      {names.length > 1 && (
        <div className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Agent</span>
          <ChipRow
            className="sky-new-agent"
            label="Agent"
            value={program}
            options={names.map((n) => ({ value: n, label: n, title: n === agents?.fallback ? "Your default agent" : undefined }))}
            onChange={setPicked}
          />
        </div>
      )}
      {error && (
        <p className="error fl-error sky-new-error" role="alert">
          {error}
        </p>
      )}
      <div className="sky-new-actions">
        <button type="button" className="test-btn fl-btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="btn-primary fl-btn sky-new-fly"
          disabled={busy || !!blocked}
          title={blocked || (attach ? "Start the session and fly “" + attach.name + "” on it" : plan ? "Start the session and fly the stops" : "Start the session")}
          onClick={() => void fly()}
        >
          {busy ? "Taking off…" : made ? "Open session" : "Fly"}
        </button>
      </div>
    </div>
  );
}
