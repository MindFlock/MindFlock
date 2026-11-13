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
 *  - More options (folded; `mf_sky_new_more` remembers it): the rest of the
 *    New dialog — account, model, workspace and base branch, template, launch
 *    flags, device (newBlockOptions.tsx). Folded, one line says what they are
 *    ("claude · opus · worktree from main", `optionsSummary`).
 *  - From a session (folded): pick a live session on this device and the new
 *    one forks from it — the body gets `parent` + `spawned`, the parent's
 *    repository, a worktree based on the parent's branch, and a worker's
 *    title (`workerTitle`: "<parent>-w1"), as `spawn_session` does. Clear
 *    goes back to the plain flow.
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

import { fullPathHint, pathLeaf } from "../../lib/serverPaths";
import { withMemoryConfirm } from "../../lib/memoryConfirm";
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../../api/client";
import type { Instance } from "../../api/types";
import { refreshInstances, useAgentChoices, useAuthProfiles, useDevices, useInstances, useSettingsDoc } from "../../state/queries";
import { rememberNewSessionChoice } from "../../state/store";
import { addPendingSession, clearStaleAlias, failPendingSession } from "../../lib/sessionActions";
import { newSessionSubmit, type NewSessionSubmitCtx } from "../../lib/newSessionSubmit";
import { attachGrounded, startFlightOn } from "../../lib/flightsApi";
import { windowName } from "../../lib/windowName";
import { deviceApi, deviceLabel, deviceTitle, startableDevices } from "../../lib/devices";
import { ensureTemplates, templatesNow, type Template } from "../../lib/templates";
import {
  defaultOptions,
  forkFields,
  forkOptions,
  optionsSummary,
  parentChoices,
  readMoreOpen,
  workerTitle,
  writeMoreOpen,
  type BlockOptions,
  type ParentChoice,
} from "../../lib/newBlock";
import { errMsg } from "../../lib/format";
import { isNameQuery, titleFromPrompt, type RepoSuggestion } from "../dialogs/newSession";
import { PathField } from "../PathField";
import { templateFiller } from "../dialogs/newSession/TemplatesStrip";
import { useBaseBranches } from "../dialogs/newSession/WorkspaceFold";
import type { RepoBranches } from "../../api/types";
import { MoreOptions, ParentPicker, optionSetter } from "./newBlockOptions";
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
  /** Made: its title, and the guess `onPending` was told (none when the
   * session was already made by an earlier press). */
  onCreated(title: string, guess?: string): void;
  onCancel(): void;
  /** Made for a grounded line's stops: they take off on the new session. */
  attach?: { id: string; name: string };
  /** Fly pressed: the optimistic row's (guessed) title is in the sessions. */
  onPending?(guess: string): void;
  /** The create was refused: the block shows again, with why. `guess` is
   * the one `onPending` was told. */
  onFailed?(guess: string): void;
  /** Drawn but not shown (its session's line already is). */
  hidden?: boolean;
}

/** How many suggested repositories show as chips. */
const REPO_CHIPS = 6;

/** The saved-stop chips: "No stops" first, then built-ins, then the owner's. */
export function savedStopChoices(plans: readonly Pick<FlightPlan, "id" | "name" | "builtin">[]): Array<{ value: string; label: string }> {
  const ordered = [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];
  return [{ value: "", label: "Nothing more" }, ...ordered.map((p) => ({ value: p.id, label: p.name }))];
}

/** The repository chips: the suggestions, recent first, one per path. */
export function repoChoices(suggestions: readonly RepoSuggestion[], max = REPO_CHIPS): Array<{ value: string; label: string; title: string }> {
  const rank = (s: RepoSuggestion) => (s.source === "recent" ? 0 : s.source === "cwd" ? 1 : 2);
  const seen = new Set<string>();
  const out: Array<{ value: string; label: string; title: string }> = [];
  for (const s of [...suggestions].sort((a, b) => rank(a) - rank(b))) {
    if (!s.path || seen.has(s.path)) continue;
    seen.add(s.path);
    out.push({ value: s.path, label: s.name || pathLeaf(s.path) || s.path, title: s.path });
    if (out.length >= max) break;
  }
  return out;
}

/** Why Fly is greyed out ("" = it can fly). */
export function flyBlocked(repo: string): string {
  if (!repo.trim()) return "Pick a repository first";
  if (isNameQuery(repo)) return "Type " + fullPathHint() + " (Browse… fills one in)";
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

/** What the block's create is made of: its rows, More options, and the
 * session it forks from (null = none). */
export interface BlockChoice {
  repo: string;
  prompt: string;
  program: string;
  launchArgs: string;
  options: BlockOptions;
  /** The session's title ("" = the server names it from the task). */
  title?: string;
  parent?: Pick<ParentChoice, "title" | "repo" | "branch"> | null;
}

/** The New dialog's create path, with the form's choices this card makes and
 * the dialog's own defaults for everything it doesn't show. A fork rides the
 * same body: its repository, a worktree on the parent's branch (forkOptions),
 * and the lineage keys (forkFields). */
export function blockBody(c: BlockChoice): Record<string, unknown> {
  const ref = <T,>(v: T) => ({ current: v });
  const o = c.parent ? forkOptions(c.options, c.parent) : c.options;
  const repo = c.parent ? c.parent.repo : c.repo;
  const ctx: NewSessionSubmitCtx = {
    title: c.title || "",
    repoPath: repo,
    prompt: c.prompt,
    program: c.program,
    planFirst: false,
    planOk: false,
    launchArgs: c.launchArgs,
    profileId: o.device ? "" : o.profileId,
    profileModel: o.device ? "" : o.profileModel,
    strategy: o.strategy,
    inPlace: o.inPlace,
    initRepo: o.initRepo,
    provision: o.provision,
    baseBranch: o.baseBranch,
    baseIsRemote: o.baseRemote,
    continueOn: "",
    activeTemplate: o.template,
    maxPieces: 0,
    splitLimit: 0,
    runMode: false,
    splitOn: false,
    laneNeedsWorktree: false,
    newFolderBlocked: "",
    provisionBlocked: "",
    draft: null as unknown as NewSessionSubmitCtx["draft"],
    laneAtPress: ref({ lane: "leave", askFirst: false }) as NewSessionSubmitCtx["laneAtPress"],
    deviceRef: ref(o.device),
    failedReopen: ref(false),
    submitArmAt: ref(0),
    setError: noop,
    setPlanError: noop,
    setPromptOpen: noop,
    setPage: noop,
    planFolderDo: noop,
    closeDialog: noop,
  };
  const body = newSessionSubmit(ctx).buildBody({
    title: c.title || "",
    repoPath: repo,
    prompt: c.prompt,
    inPlace: o.inPlace,
    initRepo: o.initRepo,
    provisioned: o.provision,
    baseBranch: o.baseBranch,
    baseBranchRemote: o.baseRemote,
  });
  return c.parent ? { ...body, ...forkFields(c.parent) } : body;
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
  const [options, setOptions] = useState<BlockOptions>(defaultOptions);
  const [moreOpen, setMoreOpen] = useState(readMoreOpen);
  const [fromOpen, setFromOpen] = useState(false);
  /** The session this one forks from (null = the plain flow). */
  const [parent, setParent] = useState<ParentChoice | null>(null);
  /** The launch flags as edited (null = the agent's saved defaults). */
  const [argsEdit, setArgsEdit] = useState<string | null>(null);
  const [templates, setTemplates] = useState<Template[]>(() => templatesNow());
  const [branches, setBranches] = useState<RepoBranches | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);

  const agents = useAgentChoices().data;
  const settings = useSettingsDoc(true).data as { coding_cli?: { default_launch_args?: Record<string, unknown> } } | undefined;
  const authProfiles = useAuthProfiles().data;
  const rows = useInstances().data;
  const startable = startableDevices(useDevices().data);
  const names = agents?.names || [];
  const program = picked ?? (agents?.fallback || names[0] || "");
  const device = parent || !startable.some((d) => d.device === options.device) ? "" : options.device;
  const shown = parent ? forkOptions(options, parent) : { ...options, device };
  const folder = parent ? parent.repo : repo;
  const dapi = useMemo(() => deviceApi(device), [device]);

  useEffect(() => {
    let live = true;
    dapi<{ suggestions?: RepoSuggestion[] }>("/api/repos/suggest")
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
  }, [dapi]);

  useEffect(() => {
    let live = true;
    ensureTemplates()
      .then((t) => live && setTemplates(t))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  // The Base branch chips for the folder in the Repository row (the New
  // dialog's own fetch); a fork's base is its parent's branch, kept.
  useBaseBranches({
    open: true,
    page: 2,
    repoPath: folder,
    dapi,
    setBaseBranch: parent ? noop : optionSetter(setOptions, "baseBranch"),
    setBranches,
  });

  useEffect(() => {
    cardRef.current?.querySelector<HTMLTextAreaElement>("textarea")?.focus();
  }, []);

  const repoChips = useMemo(() => repoChoices(suggestions), [suggestions]);
  const parents = useMemo(() => parentChoices(rows || [], windowName), [rows]);
  const blocked = flyBlocked(folder);

  const defaultArgs = (() => {
    const raw = settings?.coding_cli?.default_launch_args || {};
    const hit = Object.keys(raw).find((k) => k.toLowerCase() === program.trim().toLowerCase());
    return hit ? String(raw[hit] || "").trim() : "";
  })();
  const launchArgs = argsEdit ?? defaultArgs;

  /** Another agent: its own saved launch flags, as the New dialog does. */
  const pickProgram = (name: string) => {
    setPicked(name);
    setArgsEdit(null);
  };

  const toggleMore = () => {
    setMoreOpen((open) => {
      writeMoreOpen(!open);
      return !open;
    });
  };

  const fillTemplate = templateFiller({
    setAgent: pickProgram,
    // A fork's repository is its parent's; a template doesn't move it.
    folderDo: (a) => {
      if (a.t === "user-set" && !parent) setRepo(a.path);
    },
    setPrompt,
    setProvision: optionSetter(setOptions, "provision"),
    setStrategy: optionSetter(setOptions, "strategy"),
    setInPlace: optionSetter(setOptions, "inPlace"),
    setInitRepo: optionSetter(setOptions, "initRepo"),
    setActiveTemplate: optionSetter(setOptions, "template"),
    setAdvancedOpen: noop,
    // The block flies stops instead of a fast-track.
    setLane: noop,
    setAskFirst: noop,
  });

  const forkFrom = (c: ParentChoice) => {
    setParent(c);
    setFromOpen(false);
    // The worker runs the parent's agent when it is one of the choices.
    if (c.provider && names.includes(c.provider) && c.provider !== program) pickProgram(c.provider);
  };

  const deviceName = (d: string) => {
    const hit = startable.find((s) => s.device === d);
    return hit ? deviceLabel(hit, startable) : d;
  };
  const summary = optionsSummary({ program, options: shown, launchArgs, deviceName: deviceName(device) });

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
    const named = parent ? workerTitle(parent.title, (rows || []).map((r) => r.title)) : "";
    const body = blockBody({ repo, prompt, program, launchArgs, options: { ...options, device }, title: named, parent });
    const guess = addPendingSession(named || titleFromPrompt(prompt) || pathLeaf(folder) || "untitled", device);
    onPending?.(guess);
    let title = "";
    try {
      title = await postInstance(body, device);
      if (!device)
        rememberNewSessionChoice(String(body.repo_path || ""), {
          template: shown.template,
          program: String(body.program || ""),
          profile_id: String(body.profile_id || ""),
        });
      clearStaleAlias(title);
      await refreshInstances();
    } catch (err) {
      failPendingSession(guess);
      setError(errMsg(err));
      setBusy(false);
      onFailed?.(guess);
      return;
    }
    // The stops it flies: a grounded line's, or a saved stop picked here.
    const flew = attach ? await attachGrounded(attach.id, { title }) : plan ? await startFlightOn(title, plan) : true;
    if (!flew) {
      setMade(title);
      setError("“" + windowName(title) + "” started, but " + (attach ? "“" + attach.name + "”" : "the stops") + " didn't start — the error card says why.");
      setBusy(false);
      onFailed?.(guess);
      return;
    }
    setBusy(false);
    onCreated(title, guess);
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
        title={onMove ? "Drag to move this — the session starts where it sits" : undefined}
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
        {parent ? (
          <div className="sky-new-forked">
            <span className="sky-new-forked-text" title={parent.repo}>
              forked from <b>{parent.name}</b>
              <span className="sky-new-forked-repo">{parent.repo + (parent.branch ? " · " + parent.branch : "")}</span>
            </span>
            <button type="button" className="test-btn fl-btn sky-new-unfork" title="Back to a plain new session" onClick={() => setParent(null)}>
              Clear
            </button>
          </div>
        ) : (
          <>
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
          </>
        )}
      </div>
      <div className="fl-fld fl-fld-wide">
        <span className="fl-fld-label">Task</span>
        <textarea
          className="fl-prompt sky-new-prompt"
          aria-label="Task"
          rows={3}
          value={prompt}
          placeholder="What should the agent do? Enter runs it, Shift+Enter for a new line."
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => promptKey(e, () => void fly())}
        />
      </div>
      {plans.length > 0 && !attach && (
        <div className="fl-fld fl-fld-wide">
          <span className="fl-fld-label">Then run</span>
          <ChipRow className="sky-new-stops" label="Then run" value={plan} options={savedStopChoices(plans)} onChange={setPlan} />
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
            onChange={pickProgram}
          />
        </div>
      )}
      <div className="sky-new-folds">
        <button type="button" className="fl-icon-btn sky-new-fold sky-new-more" aria-expanded={moreOpen} onClick={toggleMore}>
          <span className="sky-new-caret" aria-hidden="true">
            {moreOpen ? "▾" : "▸"}
          </span>
          More options
        </button>
        <button
          type="button"
          className={"fl-icon-btn sky-new-fold sky-new-from" + (parent ? " is-on" : "")}
          aria-expanded={fromOpen}
          title="Start a worker forked from a running session — its repository, at its HEAD"
          onClick={() => setFromOpen((o) => !o)}
        >
          <span className="sky-new-caret" aria-hidden="true">
            {fromOpen ? "▾" : "▸"}
          </span>
          From a session
        </button>
      </div>
      {!moreOpen && (
        <p className="sky-new-summary" title="More options">
          {summary}
          {parent && " · as " + workerTitle(parent.title, (rows || []).map((r) => r.title))}
        </p>
      )}
      {fromOpen && <ParentPicker choices={parents} value={parent?.title || ""} onPick={forkFrom} />}
      {moreOpen && (
        <MoreOptions
          program={program}
          setProgram={pickProgram}
          options={shown}
          setOptions={setOptions}
          launchArgs={launchArgs}
          setLaunchArgs={setArgsEdit}
          authProfiles={authProfiles}
          branches={branches}
          templates={templates}
          onTemplate={(t) => (t ? fillTemplate(t) : setOptions((o) => ({ ...o, template: "", provision: false, strategy: "worktree", initRepo: false })))}
          devices={startable.map((d) => ({ device: d.device, label: deviceLabel(d, startable) }))}
          forkedFrom={parent?.name}
        />
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
          title={blocked || (attach ? "Start the session and run “" + attach.name + "” on it" : plan ? "Start the session, then run the saved route" : "Start the session")}
          onClick={() => void fly()}
        >
          {busy ? "Starting…" : made ? "Open session" : "Run"}
        </button>
      </div>
    </div>
  );
}

/** POST the create — here, or on the device it is aimed at, where the
 * session joins the rail under its namespaced title (the New dialog's
 * `postCreate` does the same). Resolves the session's title. */
export async function postInstance(body: Record<string, unknown>, device: string): Promise<string> {
  if (device) return deviceTitle(device, (await deviceApi(device)<Instance>("/api/instances", { json: body })).title);
  // A person's create: while memory is low the server asks first, and Start
  // anyway resends it (lib/memoryConfirm).
  const inst = await withMemoryConfirm("a new agent", async (anyway) =>
    anyway
      ? await api<Instance>("/api/instances", { json: { ...body, start_anyway: true } })
      : await api<Instance>("/api/instances", { json: body })
  );
  return inst.title;
}
