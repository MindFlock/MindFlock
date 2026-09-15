/** New-session dialog (ports section 16 + the new-form submit from section
 * 17 + the J4 preset row): quick path is name → Enter; folder browser,
 * templates strip, prompt presets, provisioning fold, and per-session launch
 * flags live behind progressive disclosure. */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import type { Config, Instance, PlanAnswer } from "../../api/types";
import { api } from "../../api/client";
import {
  refreshInstances,
  refreshConfig,
  queryClient,
  useAuthProfiles,
} from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import {
  addPendingSession,
  clearStaleAlias,
  failPendingSession,
  selectSession,
} from "../../lib/sessionActions";
import {
  BUILTIN_PRESETS,
  findPreset,
  loadUserPresets,
  saveUserPresets,
  type Preset,
} from "../../lib/presets";
import { FlagChips, tokenize } from "./FlagChips";

interface Template {
  name: string;
  program?: string;
  repo_path?: string;
  prompt?: string;
  provisioned?: boolean;
  workspace_strategy?: string;
  in_place?: boolean;
  init_repo?: boolean;
}

interface Provider {
  name: string;
  aliases?: string[];
  command?: string;
}

/** One folder /api/repos/suggest thinks the user might mean. */
interface RepoSuggestion {
  path: string;
  name: string;
  is_git: boolean;
  source: string;
}

/** The three reasons the endpoint can have for offering a folder, in the order
 * it ranks them. Each one is labelled in the strip because an unexplained list
 * of folder names reads as noise: without the label there is no way to tell the
 * repo your last session ran in from some directory that merely happens to sit
 * under $HOME, and the user can't judge a suggestion they can't account for. */
const SUGGEST_SOURCES: Array<{ key: string; label: string; hint: string }> = [
  { key: "recent", label: "Recent", hint: "folders your recent sessions ran in" },
  { key: "cwd", label: "Here", hint: "the folder MindFlock itself was started in" },
  { key: "nearby", label: "Nearby", hint: "repos and folders sitting under your home directory" },
];

/** How long the Folder field must sit still before we ask the server what it
 * is. The endpoint answers 200-with-exists:false for half-typed paths, so a
 * per-keystroke call would be harmless but pointless traffic. */
const CHECK_DEBOUNCE_MS = 400;

/** How long the Folder field must sit still before a NAME typed into it is
 * looked up. Deliberately half the path probe's wait above, because the two
 * debounces buy different things: the check only decorates a field the user has
 * already finished with, while the match list is the thing they are sitting
 * there waiting to read, and 400ms of blank space reads as "searching doesn't
 * work here". The walk behind it is bounded server-side (see
 * ``search_repos``'s scan cap and 1.5s deadline), so asking a keystroke early
 * costs a bounded amount. */
const SEARCH_DEBOUNCE_MS = 200;

/** Below this a query is not worth a walk: one character matches most of the
 * machine and tells the user nothing. ``search_repos`` enforces the same floor
 * and answers an empty list rather than an error, so this copy only saves the
 * round trip. */
const SEARCH_MIN_CHARS = 2;

/** Below this the Describe box is not worth a model turn. The floor is about
 * ambiguity, not length: one word names no project and no task, and the answer
 * to "scan" is a folder the model guessed and the user has to undo. Twenty-five
 * seconds to be told nothing useful is the worst trade this feature can make. */
const DESCRIBE_MIN_CHARS = 8;

/** What the box will send, matching the server's own MAX_SENTENCE. A pasted
 * paragraph is truncated rather than refused — a brief is still a request. */
const DESCRIBE_MAX_CHARS = 2000;

/** How long the ring runs before the label admits it. The generator's budget is
 * 75s and a cold CLI start plus a real turn is ~10s, so a spinner that never
 * changes reads as a hang long before the timeout does. */
const DESCRIBE_SLOW_MS = 8000;

/** How long Create is held after a fill lands. Focus moves when the answer
 * arrives, ~10-25s after the user pressed Enter — long enough that their hand
 * may be back on Enter for a reason that has nothing to do with this form.
 * Long enough to catch that, far short of the time it takes to read a note. */
const SUBMIT_ARM_MS = 500;

/** Whether what is in the Folder field is a PATH rather than a name to look up.
 *
 * This one predicate is what lets the field become a combobox without ceasing
 * to be a path field. Text starting with / or ~ is unambiguously a location and
 * keeps every behaviour it has always had — the check_repo probe, the git
 * nudge, Create sending it verbatim — while anything else is a name, and names
 * get searched for. There is no mode to switch and none to get stuck in: the
 * rule is re-read from the text on every keystroke, so deleting a leading slash
 * turns a path back into a search and typing one turns it back again. */
export function looksLikePath(text: string): boolean {
  const t = text.trim();
  return t.startsWith("/") || t.startsWith("~");
}

/** Whether the Folder field is holding a QUERY rather than a folder to use.
 *
 * The field means two things now, and only one of them is a location. Create
 * sends what the field holds verbatim, and the server resolves a bare name
 * against ITS OWN working directory and then CREATES it (`_prepare_plain_repo`
 * does realpath + expanduser + makedirs), so an unpicked search term — `api`
 * typed, nothing chosen, Create clicked — made a `MindFlock/api` directory and
 * started a session in it, while `backend` would have found the server's own
 * source tree. Every route to Create consults this and refuses: a name is
 * something to look up, and looking it up is what the match list is for.
 * Relative paths (`./foo`, `$HOME/foo`) are refused by the same rule and for the
 * same reason — they too resolve against the server, not against anything the
 * user can see. */
export function isNameQuery(text: string): boolean {
  const t = text.trim();
  return !!t && !looksLikePath(t);
}

/** A match's path as the list shows it under the folder's name: home-relative,
 * because every match comes from a walk rooted at $HOME and repeating
 * /home/<user>/ down the column distinguishes nothing. The ~ is kept so the
 * line still reads as a path — the row's job is to say WHERE the folder is, and
 * `code/acme/api` is vague about that in a way `~/code/acme/api` is not. Either
 * separator is honoured, since the prefix is whatever the server sent. */
export function homeRelative(path: string, home: string): string {
  if (!home || !path.startsWith(home)) return path;
  const rest = path.slice(home.length);
  if (!rest) return "~";
  return rest[0] === "/" || rest[0] === "\\" ? "~" + rest : path;
}

/** Whether a child ending at ``childRight`` fits inside a row ending at
 * ``rowRight``. Both edges must come from the SAME coordinate space — mixing
 * an offsetParent-relative offset with a container width is what emptied the
 * folder rows once already. Half a pixel of slack, since sub-pixel layout
 * routinely puts a fitting child a hair past the edge. */
export function fitsWithin(childRight: number, rowRight: number): boolean {
  return childRight <= rowRight + 0.5;
}

/** A no-wrap chip row that hides whatever doesn't fit, whole chips only.
 *
 * The row is one line by design (see NewSessionDialog.css), and `overflow:
 * hidden` alone cuts the last pill down the middle — a chopped-off folder name
 * reads as a rendering fault, not as "there are more". Only measurement can
 * tell a chip that fits from one that doesn't, so after layout each child is
 * checked against the container's right edge and the ones past it are hidden
 * outright. Hiding a later child never moves an earlier one in a no-wrap row,
 * so this settles in a single pass; a ResizeObserver redoes it when the dialog
 * is resized.
 */
function FitRow({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => {
      const kids = Array.from(el.children) as HTMLElement[];
      // Unhide first: the row may have grown, and a chip hidden at the old
      // width has no geometry to measure at the new one.
      for (const k of kids) k.classList.remove("nt-clipped");
      // Viewport coordinates for BOTH sides. offsetLeft looked like the
      // obvious measure and is a trap here: it is relative to offsetParent,
      // which for these chips is .modal (position: fixed), so each chip's
      // offset carried the whole dialog's distance from the window edge and
      // every one of them compared as overflowing — the rows rendered empty.
      const right = el.getBoundingClientRect().right;
      if (!(right > 0)) return; // not laid out yet; hide nothing
      for (const k of kids) {
        // Half a pixel of slack: sub-pixel layout would otherwise drop a chip
        // that lands exactly on the edge.
        if (!fitsWithin(k.getBoundingClientRect().right, right)) {
          k.classList.add("nt-clipped");
        }
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  });
  return (
    <div className="nt-list" ref={ref}>
      {children}
    </div>
  );
}


/** Last path segment, for naming the folder the confirm row would use. Keeps
 * both separators so a Windows path doesn't come back as the whole string. */
function leafName(path: string): string {
  return path.replace(/[/\\]+$/, "").split(/[/\\]/).pop() || path;
}

/** The Folder field's whole state, gathered into one value because both ways it
 * goes wrong are questions of ordering. Browsing writes every row the user
 * clicks straight into the field, so walking away from the browser has to put
 * back what the field held before — otherwise the last directory they merely
 * passed through becomes the session's repo_path. And the pre-fill resolves
 * after the dialog is already interactive, so it must never land on top of a
 * path typed in the meantime. folderReducer decides both, and is pure so those
 * sequences can be tested. */
export interface FolderState {
  /** What the Folder input shows, and what submit sends as repo_path. */
  path: string;
  /** Whether the folder browser sits open under the field. */
  browsing: boolean;
  /** The field as it stood when the browser opened — what Escape puts back.
   * Null when there is no browse to undo. */
  undo: { path: string; touched: boolean } | null;
  /** Whether the user has said where they want to work, by typing, clicking a
   * suggestion chip, choosing in the browser or applying a template. A pre-fill
   * that arrives after that has been outvoted. */
  touched: boolean;
}

export type FolderAction =
  /** The dialog opened: forget the last opening's browse and its touches. */
  | { t: "reopen" }
  /** The best-ranked /api/repos/suggest folder arrived. */
  | { t: "suggested"; path: string }
  /** $HOME, for a field that nothing better has filled. */
  | { t: "fallback"; path: string }
  /** The user named a folder outside the browser: typed it, clicked a
   * suggestion chip, or applied a template carrying one. */
  | { t: "user-set"; path: string }
  | { t: "browse-open" }
  /** A row was clicked or arrowed into — the selection shows in the field. */
  | { t: "browse-select"; path: string }
  /** "use this folder", a folder just created, or the panel toggled shut: the
   * deliberate finish, after which there is nothing left to undo. */
  | { t: "browse-commit"; path: string }
  /** Escape: put the field back the way the browser found it. */
  | { t: "browse-cancel" };

export const FOLDER_INIT: FolderState = {
  path: "",
  browsing: false,
  undo: null,
  touched: false,
};

/** The undo point after a pre-fill lands, which matters only mid-browse.
 *
 * Opening the browser snapshots the field as it stands, and on a first-ever open
 * that snapshot is the empty string — the user reaches for Browse… precisely
 * because the field is still blank. If the suggestion then arrives, Escape would
 * hand the field back to that empty string, and Create answers an empty folder
 * with "a folder is required" after it has already closed the dialog. So a
 * pre-fill that arrives mid-browse becomes the new "nothing happened" baseline,
 * unless the user already had a folder of their own to go back to. */
function prefillUndo(s: FolderState, path: string): FolderState["undo"] {
  if (!s.browsing) return s.undo;
  if (s.undo && s.undo.path) return s.undo;
  return { path, touched: false };
}

export function folderReducer(s: FolderState, a: FolderAction): FolderState {
  switch (a.t) {
    case "reopen":
      // The path itself survives on purpose: it is the folder the last session
      // was started in, which beats an empty field for the moment before this
      // opening's own pre-fill lands.
      return { ...s, browsing: false, undo: null, touched: false };
    case "suggested":
      // A filesystem walk that outran the user is welcome; one that comes back
      // after they typed leaves what they typed alone. An empty field has
      // nothing to lose either way, so it always gets filled.
      if (!a.path || (s.touched && s.path)) return s;
      return { ...s, path: a.path, undo: prefillUndo(s, a.path) };
    case "fallback":
      // $HOME is the weakest of the pre-fills — almost never where anyone
      // works, just somewhere the browser can start — so it only ever fills a
      // field that is otherwise empty.
      if (!a.path || s.path) return s;
      return { ...s, path: a.path, undo: prefillUndo(s, a.path) };
    case "user-set":
      // Typing while the browser is open moves the undo point with it: Escape
      // is there to cancel the browsing, and has no business also swallowing a
      // path the user wrote by hand.
      return {
        ...s,
        path: a.path,
        touched: true,
        undo: s.browsing ? { path: a.path, touched: true } : null,
      };
    case "browse-open":
      return { ...s, browsing: true, undo: { path: s.path, touched: s.touched } };
    case "browse-select":
      return { ...s, path: a.path, touched: true };
    case "browse-commit":
      return { path: a.path, browsing: false, undo: null, touched: true };
    case "browse-cancel":
      // Restores the touched flag too, so a browse that came to nothing also
      // hands the field back to a suggestion still in flight.
      return {
        path: s.undo ? s.undo.path : s.path,
        browsing: false,
        undo: null,
        touched: s.undo ? s.undo.touched : s.touched,
      };
  }
}

/** Whether the dialog's opening focus() may still claim the caret. It may not
 * once focus sits on another control inside the dialog: the user got there
 * first, and taking the caret back mid-word is how the tail of a sentence ends
 * up in the Name box. Focus outside the dialog — the menu button that opened
 * it, the terminal behind — is exactly what the dialog is there to take. */
export function mayTakeOpeningFocus(where: {
  activeIsTarget: boolean;
  activeInsideDialog: boolean;
}): boolean {
  return where.activeIsTarget || !where.activeInsideDialog;
}

/** Why a provisioned create cannot even be attempted, or "" when it can.
 *
 * Provisioning builds a SEPARATE worktree or clone, so a folder with no repo in
 * it has nothing to fork: the server answers 400 and the create never happens.
 * Answering it here rather than letting the round trip do it makes the refusal
 * instant and — more to the point — names the folder, because the git aside
 * that would otherwise explain it sits at the top of a card the user is two
 * screens below by the time they tick Provision. `initRepo` clears it: that box
 * git-inits the folder first, which is exactly the thing missing. */
export function provisionBlockReason(where: {
  provision: boolean;
  plainFolder: boolean;
  initRepo: boolean;
  folderPath: string;
}): string {
  if (!where.provision || !where.plainFolder || where.initRepo) return "";
  return (
    "Provisioning needs a git repo, and there is none in " +
    where.folderPath +
    " — pick a folder marked \u{1F4E6} above, or tick \u201CCreate a git repo in this " +
    "folder\u201D."
  );
}

/** Why an immediate "Start session now" must stop and ask, or "" when it can go.
 *
 * Page 1's whole point is skipping the form, so this is the ONE thing it does
 * not skip: a folder that is not there yet gets made by the create, and nothing
 * else in this flow produces something that outlives the session — close a
 * session and its worktree goes, but nobody ever comes back for the directory.
 * "Without validating on page 2" is about not re-reading a form; it was never
 * about creating a directory nobody was shown.
 *
 * Kept pure and separate from the handler so the one rule that makes the fast
 * path safe can be tested without a browser, and so it cannot drift from the
 * confirm row that renders the same fact. */
export function immediateStartBlockReason(where: {
  folderExists: boolean;
  folderLabel: string;
  confirmed: boolean;
}): string {
  if (where.folderExists || where.confirmed) return "";
  return (
    "One thing first: " +
    where.folderLabel +
    " does not exist yet. Tick the box, then press Start session again."
  );
}

/** Why the chosen "new worktree" will not actually happen, or "" when it will.
 *
 * A worktree is forked from a commit, so a folder with no repo in it has nothing
 * to fork: `create_instance` forces `in_place` for exactly this case and the 202
 * quietly hands back a session running in the folder. That override predates
 * this line and was always silent — tolerable while the mode was the ABSENCE
 * of a tick, and not tolerable now that it is a radio the user (or a plan) has
 * positively selected. A control reading "New worktree" over a folder that
 * cannot have one is the form promising something the server will not do.
 *
 * `initRepo` clears it for the same reason it clears provisionBlockReason: that
 * box git-inits the folder first, which is precisely the missing thing. */
export function worktreeClampReason(where: {
  inPlace: boolean;
  provisionOn: boolean;
  plainFolder: boolean;
  initRepo: boolean;
}): string {
  if (where.inPlace || where.provisionOn) return "";
  if (!where.plainFolder || where.initRepo) return "";
  return (
    "There is no git repo in that folder, so this will run in the folder itself " +
    "\u2014 tick \u201CCreate a git repo in this folder\u201D below to get a real worktree."
  );
}

/** Why the Describe box can't be sent yet, or "" when it can.
 *
 * Same pattern as provisionBlockReason above: pure, returns the sentence a
 * person reads or nothing at all, and is the ONE place the refusal lives so the
 * button and the Enter key can never disagree about it. The floor is about
 * ambiguity, not length — `search_repos("scan")` resolves to exactly one folder
 * on this machine and it is the wrong one, and there is no repair for a sentence
 * that never said which project. A busy box says nothing, because the button is
 * already saying "Reading\u2026" and a second line under it would be the dialog
 * talking over itself. */
export function describeBlockReason(where: { text: string; busy: boolean }): string {
  if (where.busy) return "";
  const t = where.text.trim();
  if (!t) return "Type what you want to work on first.";
  if (t.length < DESCRIBE_MIN_CHARS) {
    return `Say a bit more \u2014 \u201C${t}\u201D doesn't say which project or what to do.`;
  }
  return "";
}

/** Why Create is refusing right now, or "" when it isn't.
 *
 * A fill lands seconds after the keystroke that asked for it and moves the caret
 * while it does. Without this hold, an Enter aimed at nothing in particular
 * creates a session in a folder a model picked and nobody read — and submit()
 * closes the dialog optimistically before the POST, so there is nothing left on
 * screen to cancel. It says so rather than swallowing the key: a control that
 * ignores you is worse than one that explains itself. */
export function submitHoldReason(armAt: number, now: number): string {
  if (!armAt || now >= armAt) return "";
  return "Just filled the form in \u2014 check the folder, then press Create.";
}

/** The plan's in-place flag, defaulting TRUE when the key is missing.
 *
 * `!!a.in_place` reads an absent key as false, and false here means "cut a
 * branch and a worktree in somebody's repo". Of the two ways to be wrong about a
 * key that isn't there, only one of them writes to a git repo. */
export function planInPlace(a: Partial<PlanAnswer>): boolean {
  return a.in_place !== false;
}

/** The folder a landed plan is about — kept WHOLE, existing or not.
 *
 * The state this replaces held a path only while the folder still needed
 * making, which quietly made it two different facts wearing one name: the gate
 * asks "does the form still hold a folder that has to be MADE?", while the note
 * asks the wider "does the form still hold the folder I am a sentence about?".
 * Folding both into one field left the second question nothing to compare
 * against, and the note went on describing a folder the form no longer showed.
 * So the path is always the plan's repo_path, and `exists` is kept beside it. */
export interface PlanFolder {
  /** The plan's repo_path, ALWAYS — an existing folder as much as a new one. */
  path: string;
  /** The ~-relative spelling from the same answer. Cosmetic: only ever shown,
   * never compared — a display string could not match a field holding an
   * absolute path anyway. */
  display: string;
  /** Whether that folder was already on disk when the plan was made. */
  exists: boolean;
}

/** No plan has landed this opening. `exists: true` is the quiet half: an empty
 * path arms nothing either way, and true is the value that asks no question. */
export const PLAN_FOLDER_NONE: PlanFolder = { path: "", display: "", exists: true };

export type PlanFolderAction =
  /** A plan came back and was written into the form. */
  | { t: "answer"; plan: Partial<PlanAnswer> }
  /** The dialog was closed and opened again. */
  | { t: "reopen" }
  /** POST /api/instances came back 200: the folder is on disk now. */
  | { t: "created" };

/** The plan's folder across the dialog's own lifecycle.
 *
 * A reducer rather than three setState calls, for the same reason folderReducer
 * is one: every bug here has been a question of WHEN, and a transition that
 * cannot be replayed in a test is a transition nobody checked. The reopen case
 * below is exactly such a bug, and it shipped. */
export function planFolderReducer(s: PlanFolder, a: PlanFolderAction): PlanFolder {
  switch (a.t) {
    case "answer":
      // `!== true` rather than `!a.folder_exists`, for the reason planInPlace
      // gives about its own absent key: a server too old to send this (or a 200
      // that somehow lost it) then asks a question it did not need to, which
      // costs one tick — while reading a missing key as "it's already there"
      // makes a directory nobody was ever shown. Only one of those is
      // recoverable.
      return {
        path: a.plan.repo_path || "",
        display: a.plan.folder_display || "",
        exists: a.plan.folder_exists === true,
      };
    case "reopen":
      // THE ASYMMETRY, and the bug this case exists to state. The TICK dies on
      // a reopen — a "yes, make it" is consent to one sentence's folder and
      // must never carry over — but the QUESTION does not, because the field
      // the question is about survives: folderReducer's own "reopen"
      // deliberately KEEPS folder.path. Clearing the plan alongside the tick
      // disarmed the gate while the Folder field still read
      // /home/me/code/invoice-parser, and Create then made a directory nobody
      // confirmed. Nothing re-armed it either: the {t:"suggested"} dispatch
      // that would have overwritten the field is a no-op when
      // /api/repos/suggest answers an empty list — which is precisely the
      // machine whose menu was empty and whose model was therefore forced to
      // answer `new:<name>`.
      //
      // Keeping it is safe because the gate is DERIVED from the live field (see
      // newFolderGate): a retained plan self-clears the instant the field moves
      // off it, which is the whole point of the derivation.
      return s;
    case "created":
      // The create came back 200, so that directory is there now — and if it
      // did not come back, the plan's folder is not what the next opening is
      // about either. Left behind, it would have a later reopen ask to create a
      // folder that already exists.
      return PLAN_FOLDER_NONE;
  }
}

/** What the plan contributes to the confirm gate: the folder it wants MADE, or
 * "" when that folder was already there. The gate compares paths and nothing
 * else, so the "does it exist" half is answered here — in one place, so the
 * confirm row and submit()'s refusal can never disagree about it. */
export function planGatePath(p: PlanFolder): string {
  return p.exists ? "" : p.path;
}

/** The plan's note, but only while the form still shows the folder it is about.
 *
 * Derived from the field exactly as newFolderGate is, and for the same reason.
 * The note was cleared only by the next run and by a reopen, so clicking a
 * suggestion chip after a `new:` plan left "Using ~/code/invoice-parser — a new
 * folder" sitting above a Folder field, a git nudge and a Create that all said
 * something else. A sentence that disagrees with the form it explains is worse
 * than no sentence: it is the one thing on the strip a reader takes on trust,
 * and telling them a folder is about to be made when it is not is the failure
 * the server's own note_for exists to prevent. */
export function planNoteFor(where: {
  /** The server's sentence about what it chose and why. */
  note: string;
  /** The plan's repo_path — its folder, existing or not. */
  planPath: string;
  /** What the Folder field holds RIGHT NOW. */
  folderPath: string;
}): string {
  const path = where.planPath.trim();
  if (!path || where.folderPath.trim() !== path) return "";
  return where.note;
}

/** The one in-flight plan request, as a value instead of two loose refs.
 *
 * `seq` is the staleness stamp every landing checks; `abort` is the socket.
 * They live in one object because both bugs here were one of them moving
 * without the other — a seq bumped with the controller left orphaned, and a
 * close that moved neither. */
export interface PlanRun {
  seq: number;
  abort: AbortController | null;
}

/** Claim the slot for a new run, or refuse because one is already running.
 *
 * The refusal cannot live in describeBlockReason: that one answers "" while a
 * turn is in flight ON PURPOSE, because the button is already saying "Reading…"
 * and a second line under it would be the dialog talking over itself. So Enter
 * — which goes on firing, the box being readOnly rather than disabled, so it
 * keeps focus — fell straight through it, and every press bought another POST
 * /api/session-plan: three more 1.5s filesystem walks and another headless CLI
 * turn, bounded only by the server's 75s budget and by nothing at all on the
 * concurrency side. Five impatient Enters were five concurrent CLI turns. The
 * mobile sheet has had this guard since it shipped (`if (planBusy) return`).
 *
 * Aborts whatever it is replacing BEFORE taking the slot, so no path can orphan
 * a request: the old code overwrote the controller without aborting it, and an
 * orphaned controller is a socket nobody can ever cancel. */
export function startPlanRun(
  run: PlanRun,
  busy: boolean
): { seq: number; ctl: AbortController } | null {
  if (busy) return null;
  run.abort?.abort();
  const ctl = new AbortController();
  run.seq += 1;
  run.abort = ctl;
  return { seq: run.seq, ctl };
}

/** Stop waiting: the Cancel button, a reopen, and — the case that was missing —
 * the dialog simply being CLOSED.
 *
 * The seq bump is what actually cancels, by making the answer a no-op wherever
 * it lands; the abort only saves the socket. This component never unmounts (it
 * returns null when shut), so a close registered no cleanup whatsoever: ~15s
 * later the answer arrived with its staleness check intact and applyPlan wrote
 * the model's path into the folder reducer behind a dialog nobody was looking
 * at. */
export function cancelPlanRun(run: PlanRun): void {
  run.seq += 1;
  run.abort?.abort();
  run.abort = null;
}

/** The folder a plan wants MADE and the user has not agreed to yet, spelled for
 * a person — or "" when there is nothing to agree to.
 *
 * Creating a directory is the one thing a plan proposes that outlives the
 * session and that closing it never takes back: a worktree goes when the session
 * does, and nobody ever comes back for the folder. So a folder a MODEL invented
 * has to be confirmed in as many words before Create will run. A folder the USER
 * typed is not gated and must not be — this dialog has always made one on Create,
 * and that is their own act.
 *
 * DERIVED FROM THE FIELD, never a flag that gets cleared, and that is the whole
 * design. The Folder field is written from six places (typing, the browse tree's
 * select / commit / cancel, a suggestion chip, a search match, a template) and a
 * boolean cleared at five of them is a gate that silently survives onto a folder
 * the plan never proposed — which is precisely the create nobody confirmed. Here
 * the question exists exactly while the field still holds the plan's own path, so
 * editing the field to anything else ends it, and typing that path back starts it
 * again: the field holds the model's folder, so the model's folder is what Create
 * would make.
 *
 * Falls back to the absolute path when the server sent no display spelling. A
 * missing cosmetic field must never be able to switch the gate off — of the two
 * ways to be wrong here, only one of them makes a directory nobody read about. */
export function newFolderGate(where: {
  /** The absolute repo_path the plan proposed, "" when its folder already
   * exists (or when no plan has landed this opening). */
  planPath: string;
  /** The ~-relative spelling to show, from the same answer. */
  planDisplay: string;
  /** What the Folder field holds RIGHT NOW. */
  folderPath: string;
}): string {
  const path = where.planPath.trim();
  if (!path || where.folderPath.trim() !== path) return "";
  return where.planDisplay.trim() || path;
}

/** Why Create is refusing an unconfirmed new folder, or "" when it isn't.
 *
 * Same shape as provisionBlockReason above, and one place for the same reason:
 * the button, Ctrl/Cmd+Enter and Enter in any field all reach submit() and must
 * never disagree about this. It NAMES the folder and quotes the tick verbatim,
 * because this card scrolls — by the time someone presses Create the confirm row
 * can be several screens up, and "confirm the folder first" would be an
 * instruction about a control they cannot see. */
export function newFolderBlockReason(where: { gate: string; confirmed: boolean }): string {
  if (!where.gate || where.confirmed) return "";
  return (
    "There is no folder at " +
    where.gate +
    " yet — tick “Yes, create " +
    where.gate +
    "” under “Describe it” to have Create make it, or put a folder that " +
    "already exists in Folder."
  );
}

export function NewSessionDialog() {
  const open = useUi((s) => s.openDialog === "new-session");
  const closeDialog = useUi((s) => s.closeDialog);

  const [title, setTitle] = useState("");
  const [program, setProgram] = useState("");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [prompt, setPrompt] = useState("");
  const [launchArgs, setLaunchArgs] = useState("");
  const [provision, setProvision] = useState(false);
  const [strategy, setStrategy] = useState("worktree");
  const [inPlace, setInPlace] = useState(true);
  const [initRepo, setInitRepo] = useState(false);
  const [error, setError] = useState("");
  // "Git & workspace" starts OPEN — hiding those choices behind a click had
  // people launch with the wrong strategy rather than discover it, and they
  // are the settings this dialog exists to set. Launch flags stay
  // CLOSED: extra CLI flags are a per-session rarity, and the card is sized
  // for the form without them. Either fold's state is remembered for the life
  // of the dialog.
  const [advancedOpen, setAdvancedOpen] = useState(true);
  const [launchOpen, setLaunchOpen] = useState(false);
  // The prompt is a fold too, and a closed one: a session started with no
  // prompt is the common case (you drive it by hand from the terminal), and
  // the textarea plus its preset row was the tallest block on the card. It
  // sits BELOW the git/workspace options, where the things you set before
  // launching are grouped.
  const [promptOpen, setPromptOpen] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [activeTemplate, setActiveTemplate] = useState("");
  // The one-sentence door at the top of the body: what the user typed, whether
  // a model turn is in flight, and whether that turn has gone on long enough
  // that the ring alone stops reading as progress. Deliberately NOT wired to
  // anything the form submits — the sentence's only output is the fields
  // applyPlan writes, and it is kept only so the box still shows it afterwards.
  /** Which of the two pages is on screen: 1 the sentence, 2 the form.
   *
   * An opening lands on 1. That is a deliberate reversal of "Ctrl+N, type a
   * name, Enter" — the sentence is now the front door and the form is behind
   * it — so page 1 carries an explicit way through to the form that costs one
   * click and no model turn, and a failed create reopens on page 2, where the
   * fields to fix it are. */
  const [page, setPage] = useState(1);
  const [describe, setDescribe] = useState("");
  const [describing, setDescribing] = useState(false);
  const [describeSlow, setDescribeSlow] = useState(false);
  // The server-composed sentence about what it chose and why, and the inline
  // failure. Two states rather than one because they are different registers
  // in the same place: the note is muted, the error is red, and a run that
  // fails must not leave the previous run's note underneath claiming a folder.
  const [planNote, setPlanNote] = useState("");
  const [planError, setPlanError] = useState("");
  // The folder the last plan was about — always its repo_path, existing or not
  // — and whether the user has said yes to making it. Both the gate and the
  // note are derived by comparing that path against the Folder field (see
  // newFolderGate / planNoteFor), which is why the plan is kept whole and why
  // its transitions live in a reducer that can be replayed in a test.
  const [planFolder, planFolderDo] = useReducer(planFolderReducer, PLAN_FOLDER_NONE);
  const [newFolderOk, setNewFolderOk] = useState(false);
  /** The last plan and the sentence it answered, so page 1's "Start session"
   * can be pressed twice without paying for a second model turn.
   *
   * It needs two presses whenever the plan lands on a folder that does not
   * exist: the first shows the question, the second acts on the answer. Without
   * this the second press would re-run the CLI and could come back with a
   * different folder than the one the user just agreed to — a confirmation for
   * one directory spent on another. */
  const lastAnswer = useRef<{ sentence: string; answer: PlanAnswer } | null>(null);
  /** Set when a fill has just moved to page 2 and the Folder field, which only
   * exists on that page, still has to take the caret. */
  const focusFolderNext = useRef(false);
  /** Set when applyPlan is about to open the Prompt fold, so that fold's
   * onToggle can tell a programmatic open from a click.
   *
   * A <details> fires `toggle` whichever way it was opened — React setting the
   * `open` attribute counts — and that handler scrolls the fold to the bottom
   * of the scroll region. So a plan that filled in a prompt scrolled page 2
   * down the moment it arrived, undoing the "land at the top of the form" the
   * focus effect had just done and cutting Name and Folder off above the view.
   * (The launch fold's own comment claims an open-by-default fires no toggle;
   * that is true of the initial render and not of this.) */
  const foldOpenedByPlan = useRef(false);
  const [provisioningAvailable, setProvisioningAvailable] = useState(false);
  const [homePath, setHomePath] = useState("");
  const [suggestions, setSuggestions] = useState<RepoSuggestion[]>([]);
  // The server's verdict on the folder in the field, stamped with the path we
  // asked about — see the debounce effect for why the stamp is load-bearing.
  const [folderCheck, setFolderCheck] = useState<{ asked: string; plain: boolean } | null>(null);
  // What /api/repos/search last answered, stamped with the query it answers.
  // Same trick as folderCheck, load-bearing for the same reason: a walk started
  // three keystrokes ago must not drop its matches under a field that has moved
  // on. `home` rides along so the rows can shorten their paths against the
  // server's idea of home rather than the config's.
  const [search, setSearch] = useState<{
    asked: string;
    matches: RepoSuggestion[];
    truncated: boolean;
    home: string;
  } | null>(null);
  // Whether the match list is on screen. Escape closes it and touches nothing
  // else: the list is a suggestion, not a modal, and dismissing it must not also
  // throw away the query the user typed to summon it.
  const [searchOpen, setSearchOpen] = useState(true);
  // Which match Enter takes. Always a real row rather than "nothing selected",
  // so what Enter will do is visible before it is pressed — the alternative is a
  // key that either fills the field or creates a session depending on state the
  // user can't see.
  const [searchSel, setSearchSel] = useState(0);
  const [presetValue, setPresetValue] = useState("");
  const [savedPresets, setSavedPresets] = useState<Preset[]>([]);
  // Auth profile pin: "" = inherit the app-wide default account; "default" =
  // explicitly the CLI's own login; anything else = a configured profile id.
  const [profileId, setProfileId] = useState("");
  // This session's model override of the account's own pin ("" = the pin).
  const [profileModel, setProfileModel] = useState("");
  // Model ids the selected OpenRouter key can reach, per profile id — fetched
  // lazily on selection so the Model field is a picker, not a guess.
  const [profileModels, setProfileModels] = useState<Record<string, string[]>>({});
  const authProfiles = useAuthProfiles().data;
  const selectedProfile = (authProfiles?.profiles || []).find((p) => p.id === profileId);
  const launchDefaults = useRef<Record<string, string>>({});
  const titleRef = useRef<HTMLInputElement | null>(null);
  const describeRef = useRef<HTMLInputElement | null>(null);
  // #new-repo-path, so a landed plan can put the caret on the one field most
  // likely to be wrong — and, because focusing scrolls, actually on screen.
  const repoRef = useRef<HTMLInputElement | null>(null);
  /** The in-flight plan request: a seq bumped by every run, every open and
   * every CLOSE, so an answer that belonged to a sentence the user has moved on
   * from can never land on the form, plus the controller that drops its socket.
   * Same job as the `live` flags in the check and search effects below, and
   * needed for the same reason: this component never unmounts (it returns null
   * when shut), so a promise started before a close is still running after the
   * reopen. One object rather than two refs because every bug here has been one
   * of the two moving without the other — see startPlanRun / cancelPlanRun. */
  const planRun = useRef<PlanRun>({ seq: 0, abort: null });
  /** Epoch after which Create is armed again. See submitHoldReason. */
  const submitArmAt = useRef(0);
  const launchRef = useRef<HTMLDetailsElement | null>(null);
  // The match list's scroll box, so the keyboard highlight can be scrolled into
  // it — see the effect below for why nothing else will do that.
  const searchListRef = useRef<HTMLDivElement | null>(null);
  const promptRef = useRef<HTMLDetailsElement | null>(null);
  // The modal's own element, so the opening focus can tell "nothing in here has
  // the caret yet" from "the user is already typing in one of these fields".
  const rootRef = useRef<HTMLDivElement | null>(null);
  // Armed by a failed create, for the reopen it is about to trigger. See the
  // reset effect, which is the thing that has to know.
  const failedReopen = useRef(false);
  // The Folder field and the browser's visibility travel together: see
  // folderReducer for the two orderings that forced them into one value. These
  // aliases keep the form's many readers reading a plain value, while every
  // write goes through the reducer.
  const [folder, folderDo] = useReducer(folderReducer, FOLDER_INIT);
  const repoPath = folder.path;
  const browserOpen = folder.browsing;

  // Reset + load fresh data on every open (matches openDialog()) — except the
  // one open that is a failed create's own reopen.
  //
  // That path arrives here a tick after `submit` set the error, so this reset
  // used to wipe the only report of the failure the user ever gets, along with
  // the name, the prompt and the Provision tick they would have had to retype.
  // A refused create was therefore indistinguishable from the New Session menu
  // reopening itself for no reason. Nothing below needs redoing on that path
  // either: the component never unmounts, so every list this effect loads is
  // still in state from the open the user actually made.
  useEffect(() => {
    if (!open) {
      // A CLOSE cancels the plan too, and nothing used to do it. The reset
      // below was the only thing that bumped the seq, and it runs on OPEN — so
      // closing the dialog registered no cleanup at all, and since this
      // component never unmounts the answer landed ~15s later with its
      // staleness check still passing, writing the model's folder into a form
      // that was not on screen. Cancel first, return second.
      cancelPlanRun(planRun.current);
      return;
    }
    if (failedReopen.current) {
      failedReopen.current = false;
      return;
    }
    setTitle("");
    setError("");
    setPrompt("");
    setLaunchArgs("");
    // The Describe box and everything it leaves behind. Each of these is here
    // because a useState without a reset line leaks from one opening to the
    // next — `strategy` is the standing counter-example — and a leaked note is
    // worse than a leaked field: it is a sentence about a folder the form is no
    // longer showing.
    setDescribe("");
    setDescribing(false);
    setDescribeSlow(false);
    setPlanNote("");
    setPlanError("");
    // The tick, and ONLY the tick. Last opening's "yes, make that folder" is
    // consent to one sentence's folder and must never carry over, so it dies
    // here — but the question it answered does not, because the field the
    // question is about survives: the folder reducer's "reopen" deliberately
    // KEEPS folder.path. Clearing the plan alongside the tick is what disarmed
    // the gate over a Folder field still holding the model's not-yet-existing
    // folder, and Create then made a directory nobody confirmed. See
    // planFolderReducer's "reopen" for the asymmetry and why keeping it is
    // safe: the gate is derived from the live field, so a retained plan
    // self-clears the moment that field moves off it.
    // Every opening starts at the sentence. The form is one click away and
    // keeps whatever the last opening left in it only as far as the resets
    // below allow — page is not one of the things worth remembering, because a
    // dialog that reopens halfway through a flow nobody is in the middle of
    // reads as a bug.
    setPage(1);
    setNewFolderOk(false);
    planFolderDo({ t: "reopen" });
    // An answer for last opening's sentence has nothing to say about this one,
    // and a model turn is slow enough to still be running when the dialog is
    // closed and reopened. Bumping the seq is what makes the late one a no-op;
    // the abort is the courtesy of not waiting for it.
    cancelPlanRun(planRun.current);
    // Nothing has been filled in yet, so nothing is holding Create — a hold
    // carried over from last opening would refuse the first Enter of this one.
    submitArmAt.current = 0;
    setProvision(false);
    setInPlace(true);
    setInitRepo(false);
    // Matches the initial state, and has to be set here too: this reset runs
    // on EVERY open, so a useState default alone left the fold shut from the
    // second open onward.
    setAdvancedOpen(true);
    setLaunchOpen(false);
    setPromptOpen(false);
    folderDo({ t: "reopen" });
    // Last opening's matches answered last opening's query, and the reducer's
    // "reopen" keeps the folder itself — so the list would come back up over a
    // field nobody has typed into yet.
    setSearch(null);
    setSearchOpen(true);
    setSearchSel(0);
    setActiveTemplate("");
    setPresetValue("");
    setProfileId("");
    setProfileModel("");
    // Refetched per open: the dialog outlives the page's whole session, and a
    // rotated OpenRouter key must not keep offering the old key's catalog.
    setProfileModels({});
    setSavedPresets(loadUserPresets());
    let live = true;
    // The folder suggestions get a request of their own rather than a place in
    // the barrier below, because the endpoint walks the filesystem: hundreds of
    // listdir/stat probes under $HOME plus a git rev-parse per surviving
    // candidate, which is milliseconds warm and seconds on a cold or
    // network-mounted home. Behind the barrier that walk would hold the agent
    // list, the launch flags and the templates hostage to it as well.
    (async () => {
      try {
        const d = await api<{ suggestions?: RepoSuggestion[] }>("/api/repos/suggest");
        if (!live) return;
        const sug = d.suggestions || [];
        setSuggestions(sug);
        // Start on the best-ranked one — normally the folder the last session
        // used — so the common case needs no Browse trip at all. Unless the user
        // has already said where they want to work: see folderReducer.
        folderDo({ t: "suggested", path: sug[0]?.path || "" });
      } catch {
        // Suggestions are sugar. The field, the browser and Create all work
        // without them, so a failed walk must not cost the dialog anything.
        if (live) setSuggestions([]);
      }
    })();
    (async () => {
      // Config, settings and templates all answer out of memory, so waiting for
      // the slowest of the three costs the dialog nothing.
      const [cfgR, setR, tplR] = await Promise.allSettled([
        refreshConfig().then(() => queryClient.getQueryData<Config>(["config"])),
        api<{ settings?: { coding_cli?: { default_launch_args?: Record<string, string> } } }>(
          "/api/settings"
        ),
        api<{ templates?: Template[] }>("/api/templates"),
      ]);
      if (!live) return;
      const cfg = cfgR.status === "fulfilled" ? cfgR.value : undefined;
      setHomePath(cfg?.home || "");
      // $HOME is the fallback it always was: almost never what the user wants,
      // but a place the browser can start from when the suggestions had nothing
      // to offer (or failed, or are still walking).
      folderDo({ t: "fallback", path: cfg?.home || "" });
      setProvisioningAvailable(!!cfg?.provisioning_available);
      let provs: Provider[] = [];
      try {
        const d = await api<{ providers?: Provider[] }>("/api/providers/manage");
        provs = d.providers || [];
      } catch {
        /* providers are optional */
      }
      if (!live) return;
      setProviders(provs);
      // Map the saved default (name / alias / raw command) to the provider NAME.
      const prev = cfg?.default_program || "";
      const lower = prev.toLowerCase();
      const match = provs.find(
        (p) =>
          (p.name || "").toLowerCase() === lower ||
          (p.aliases || []).some((a) => String(a).toLowerCase() === lower) ||
          String(p.command || "").toLowerCase() === lower
      );
      const agent = match ? match.name : prev;
      setProgram(agent);
      // Per-provider default launch flags pre-fill the field so the default
      // chips start ON; the field is sent explicitly, so toggling one off
      // for this session is honored server-side.
      const raw =
        (setR.status === "fulfilled" && setR.value?.settings?.coding_cli?.default_launch_args) ||
        {};
      launchDefaults.current = {};
      for (const k of Object.keys(raw))
        launchDefaults.current[k.toLowerCase()] = String(raw[k] || "");
      setLaunchArgs((launchDefaults.current[agent.trim().toLowerCase()] || "").trim());
      setTemplates(tplR.status === "fulfilled" ? tplR.value?.templates || [] : []);
    })();
    return () => {
      // Answers for a dialog that has since been closed (or closed and reopened)
      // have nothing to say about this opening, and the suggestion walk is slow
      // enough to still be running when that happens.
      live = false;
    };
  }, [open]);

  // The opening focus belongs to the opening itself, not to the back of the
  // loads above: the dialog paints and is typeable the moment `open` flips, and
  // a focus() that waits for a filesystem walk lands seconds later, yanking the
  // caret out of the Prompt box mid-sentence so the rest of the sentence goes
  // into Name. Even at this range something can get there first — a click that
  // beats this effect's commit — and whatever did keeps the caret.
  useEffect(() => {
    if (!open) return;
    // The sentence box on page 1, the Name field on page 2 — whichever of the
    // two the opening is actually showing. titleRef is null on page 1 now (that
    // field is not rendered), and an opening that focused nothing would leave
    // the caret wherever the app last had it, outside the modal.
    const el = page === 1 ? describeRef.current : titleRef.current;
    const active = document.activeElement;
    if (
      el &&
      mayTakeOpeningFocus({
        activeIsTarget: active === el,
        activeInsideDialog: !!active && !!rootRef.current?.contains(active),
      })
    )
      el.focus();
    // `page` is deliberately NOT a dependency: this is the OPENING's focus, and
    // re-running it on every page change would fight the two focus moves that
    // belong to the flow itself — the Folder field after a fill, and whatever
    // the user had clicked before pressing Back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Whether the folder is a git repo is only knowable server-side, and the
  // field is free text, so it has to be re-asked as the user types. Two things
  // keep that honest: the timer, which waits for the typing to settle, and the
  // `live` flag, which the cleanup clears so a slow answer for an abandoned
  // path can never land on top of a newer one — the nudge appearing under a
  // folder that already has a repo is exactly the wrong kind of wrong.
  useEffect(() => {
    const asked = repoPath.trim();
    // Only a PATH is probed. The field also takes a name to look up now, and a
    // name is not a location: `check_repo` resolves whatever it is handed
    // against the SERVER's working directory, so typing `backend` while the
    // server runs from its own checkout answered "exists, no git repo here" —
    // about MindFlock's own backend/ — and the nudge underneath offered to git
    // init it. The search list is the right answer to a name; a status line
    // about some directory beside the server is not.
    if (!open || !asked || !looksLikePath(asked)) {
      setFolderCheck(null);
      return;
    }
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const r = await api<{ exists?: boolean; is_dir?: boolean; is_git?: boolean }>(
          "/api/repos/check?path=" + encodeURIComponent(asked)
        );
        if (live) setFolderCheck({ asked, plain: !!r.exists && !!r.is_dir && !r.is_git });
      } catch {
        // A folder we can't probe simply gets no aside. Guessing "no git here"
        // from a failed request would nag people into initialising repos they
        // already have.
        if (live) setFolderCheck(null);
      }
    }, CHECK_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [open, repoPath]);

  // A NAME in the Folder field is a question — "where is the repo called api?"
  // — and only the server can answer it: the suggestion sweep above is depth-1
  // by design, so a repo three levels down is invisible to it. Same machinery as
  // the check probe above (a timer so the walk waits for the typing to settle, a
  // `live` flag so an overtaken answer can never land) plus the one guard that
  // keeps this field a path field: text starting with / or ~ never reaches the
  // endpoint at all, so a typed path behaves exactly as it did before this list
  // existed.
  useEffect(() => {
    const asked = repoPath.trim();
    if (!open || looksLikePath(asked) || asked.length < SEARCH_MIN_CHARS) {
      setSearch(null);
      return;
    }
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const r = await api<{ matches?: RepoSuggestion[]; truncated?: boolean; home?: string }>(
          "/api/repos/search?q=" + encodeURIComponent(asked)
        );
        if (!live) return;
        setSearch({
          asked,
          matches: r.matches || [],
          truncated: !!r.truncated,
          home: r.home || "",
        });
        // The newly ranked top row is what Enter should take. Leaving the
        // highlight where the last query left it would aim it at whichever
        // folder happens to sit at that index now — a different folder, chosen
        // by nobody.
        setSearchSel(0);
      } catch {
        // A search that failed says nothing rather than "no matches": there is a
        // difference between "your folder isn't there" and "we couldn't look",
        // and only the first of those should send someone to Browse….
        if (live) setSearch(null);
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [open, repoPath]);

  // The highlighted match has to be ON SCREEN, and only this can put it there.
  // The list is a short scroller (about five rows, see .nf-search-list) holding
  // up to twenty matches, so a few presses of ArrowDown walk the highlight out
  // of the visible box: every row still on screen looks unselected, and Enter
  // then fills the field from a folder the user was never shown. Nothing
  // scrolls by itself either — the caret stays in the input and the list is
  // driven by aria-activedescendant, which moves no viewport.
  useEffect(() => {
    const row = searchListRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    // "nearest" scrolls the minimum: the highlight comes into view without the
    // list jumping under a user who is only stepping one row at a time. Guarded
    // because scrollIntoView is a real-browser nicety jsdom does not implement,
    // and a missing scroll must not throw inside a render commit.
    if (row && typeof row.scrollIntoView === "function") row.scrollIntoView({ block: "nearest" });
  }, [searchSel, search, searchOpen]);

  const setAgent = useCallback((value: string) => {
    const v = (value || "").trim();
    setProgram(v);
    // Switching agents resets the flags to that provider's saved default.
    setLaunchArgs((launchDefaults.current[v.toLowerCase()] || "").trim());
  }, []);

  /** The canonical provider name behind whatever is in the Agent field —
   * mirrors the backend's resolve(): basename of the executable token,
   * matched against each provider's name/aliases/command. Without this a
   * path ("/usr/local/bin/claude") or an alias ("agy") the backend routes
   * fine would trip the no-route warning and get auto-overwritten. */
  const canonAgent = useCallback(
    (raw: string): string => {
      const tok = (raw.trim().split(/\s+/)[0] || "").toLowerCase();
      const base = tok.split("/").pop() || tok;
      const m = providers.find(
        (p) =>
          p.name.toLowerCase() === base ||
          (p.aliases || []).some((a) => String(a).toLowerCase() === base) ||
          String(p.command || "").toLowerCase() === base
      );
      return m ? m.name : base;
    },
    [providers]
  );

  /** Picking an account steers the Agent field: with an OpenRouter (or any
   * key) account the identity is the choice that matters, so an agent the
   * account can't route is auto-swapped to one it can — the alternative is a
   * session that silently launches on the CLI's own login. A profile with raw
   * env overrides applies to every CLI, so it never steers. */
  const setAccount = (id: string) => {
    setProfileId(id);
    setProfileModel("");
    const prof = (authProfiles?.profiles || []).find((p) => p.id === id);
    if (!prof) return;
    const supported = prof.supported_agents || [];
    const hasEnv = !!prof.env && Object.keys(prof.env).length > 0;
    if (supported.length && !hasEnv && !supported.includes(canonAgent(program))) {
      const preferred =
        prof.provider && supported.includes(prof.provider) ? prof.provider : supported[0];
      setAgent(preferred);
    }
    // OpenRouter accounts get a model picker: ask the key what it can reach
    // (cached per profile; a failed fetch just leaves the free-text field).
    if (prof.kind === "openrouter" && !profileModels[id]) {
      (async () => {
        try {
          const r = await api<{ ok?: boolean; models?: string[] }>(
            "/api/settings/test/openrouter",
            { json: { profile_id: id } }
          );
          if (r?.ok && r.models?.length)
            setProfileModels((m) => ({ ...m, [id]: r.models || [] }));
        } catch {
          /* the free-text input still works */
        }
      })();
    }
  };

  // The route warning for the CURRENT combination (the auto-swap above keeps
  // this rare — it appears when the user manually re-picks an unrouted agent).
  const routeWarning = (() => {
    if (!selectedProfile) return "";
    const supported = selectedProfile.supported_agents || [];
    const hasEnv = !!selectedProfile.env && Object.keys(selectedProfile.env).length > 0;
    if (hasEnv || !supported.length) return "";
    if (supported.includes(canonAgent(program))) return "";
    return (
      `“${selectedProfile.label || selectedProfile.id}” has no route for ${program || "this agent"} — ` +
      `the session would run on the CLI's own login. It works with: ${supported.join(", ")}.`
    );
  })();

  const fillFromTemplate = (t: Template) => {
    if (t.program) setAgent(t.program);
    if (t.repo_path) folderDo({ t: "user-set", path: t.repo_path });
    if (t.prompt) setPrompt(t.prompt);
    setProvision(!!t.provisioned);
    if (t.workspace_strategy) setStrategy(t.workspace_strategy);
    // A provisioned template's in_place is dead on arrival — the server drops it
    // (`in_place = … and not is_provisioned`) — and taking it at face value here
    // would tick the two boxes that now exclude each other, leaving the fold in
    // a state the user cannot get out of and the form claiming a mode the
    // session will not run in. init_repo, by contrast, is now free to arrive
    // alongside either: git-initialising a folder and working directly in it is
    // an ordinary combination.
    setInPlace(!!t.in_place && !t.provisioned);
    setInitRepo(!!t.init_repo);
    setAdvancedOpen(!!(t.provisioned || t.init_repo || !t.in_place));
    // A template that brings a prompt has just written into a fold that
    // defaults shut; leaving it shut hides the text it filled in.
    if (t.prompt) setPromptOpen(true);
    setActiveTemplate(t.name);
    if (!title.trim()) setTitle(t.name || "");
    titleRef.current?.focus();
  };

  /** Write a plan from the Describe box into the form the user is looking at.
   *
   * Deliberately NOT fillFromTemplate, whose last third is wrong for this caller
   * three ways. Its `setAdvancedOpen(!!(t.provisioned || t.init_repo ||
   * !t.in_place))` evaluates to FALSE for the commonest plan there is — an
   * existing repo, in place, no init — so reusing it would CLOSE "Git &
   * workspace" and hide the two checkboxes the model had just decided, which is
   * the exact opposite of what this box is for. Its `setProvision(!!...)` would
   * silently untick a Provision box the user armed by hand. And its
   * `setActiveTemplate(t.name)` would write a session title into the state that
   * highlights a template chip, lighting up whichever template happens to share
   * the name.
   *
   * Every field is written unconditionally (except the title — see below),
   * because the button says it replaces the form, and a half-replaced form is
   * one whose leftover values are indistinguishable from the new ones. */
  const applyPlan = (a: PlanAnswer) => {
    setError("");
    // Through the reducer like every other way of naming a folder, so the rest
    // follows by itself: the debounced /api/repos/check probe fires on the new
    // path (it is absolute, so looksLikePath waves it through), the git nudge
    // appears under a plain folder, the matching suggestion chip lights up, and
    // the name-search effect clears its matches rather than leaving a stale list
    // hanging under a path.
    folderDo({ t: "user-set", path: a.repo_path });
    // A blank title is the server declining to name the session, not an
    // instruction to erase the name the user typed before reaching for the box.
    if (a.title) setTitle(a.title);
    setPrompt(a.prompt || "");
    setInPlace(planInPlace(a));
    setInitRepo(!!a.init_repo);
    // The whole folder, existing or not: the gate reads the "does it need
    // making" half of it and the note reads the path. Keeping a path only for a
    // NEW folder is what left the note with nothing to compare itself against.
    // Reading a MISSING folder_exists as "not there" lives in the reducer,
    // beside the comment about why.
    planFolderDo({ t: "answer", plan: a });
    // ALWAYS false, including over a plan that proposes the same folder as the
    // last one: a tick left over from the previous sentence is consent to that
    // sentence's folder, not to this one's. Every fill asks again.
    setNewFolderOk(false);
    // The plan never provisions — `provisioned` is not in the wire contract and
    // the server never emits it — so this is a clear, not a copy.
    setProvision(false);
    // Never CLOSE a fold the plan has just written into. Unconditional on
    // purpose; see the fillFromTemplate comparison above.
    setAdvancedOpen(true);
    if (a.prompt) {
      foldOpenedByPlan.current = true;
      setPromptOpen(true);
    }
  };

  /** Stop waiting. The seq bump is what actually cancels — it makes the answer
   * a no-op whenever it lands — and the abort only saves the socket. The
   * subprocess on the far side is not killed by this and runs to its own
   * timeout; it is a read-only one-shot with stdin closed, so that is bounded
   * and harmless rather than something worth building a kill channel for. */
  /** Page 1's "Start session": plan if we need to, otherwise act on the plan
   * we already have.
   *
   * The second press of this button — the one after ticking a new folder's
   * confirm — must NOT buy another model turn. Beyond the waste, a fresh turn
   * could answer with a different folder than the one the user just said yes
   * to, which would spend a confirmation on a directory nobody was shown. The
   * cached answer is keyed on the sentence, so editing the box does correctly
   * force a new plan. */
  const startNow = () => {
    const cached = lastAnswer.current;
    if (cached && cached.sentence === describe.trim()) {
      void startFromPlan(cached.answer);
      return;
    }
    void runDescribe("start");
  };

  /** Move the caret to the Folder field once page 2 is actually on screen.
   *
   * runDescribe cannot do this itself: it sets the page and the field it wants
   * is rendered by that same update, so repoRef.current is still null on the
   * line after. Focusing also SCROLLS the field into view, which is half the
   * point — a filled-in form is taller than the sentence that asked for it. */
  useEffect(() => {
    if (page !== 2 || !focusFolderNext.current) return;
    focusFolderNext.current = false;
    const el = repoRef.current;
    if (!el) return;
    // Top of the form FIRST, then the caret without moving it again.
    // focus() scrolls its target into view all by itself, which landed page 2
    // already scrolled — Folder pinned to the top edge and Name cut off above
    // it, so the page you had just been sent to appeared to start in the
    // middle. preventScroll keeps the caret where the value most likely to be
    // wrong is, while the page still reads from its own beginning.
    const body = el.closest(".nf-body");
    if (body) body.scrollTop = 0;
    el.focus({ preventScroll: true });
    el.setSelectionRange(el.value.length, el.value.length);
  }, [page]);

  const cancelDescribe = () => {
    cancelPlanRun(planRun.current);
    setDescribing(false);
    setDescribeSlow(false);
  };

  /** Ask the server to read the sentence and fill the form in. */
  /** Start an immediate create from a plan, or refuse and say why.
   *
   * Page 1's "Start session" is the "don't make me read the form" path, and it
   * skips page 2 entirely — but it does NOT skip the one question that is not a
   * validation: a folder that does not exist yet still has to be agreed to in
   * as many words, because nothing else in this flow makes something that
   * outlives the session. So the first press shows the question and stops, and
   * the second press — with the tick on, and reusing the answer rather than
   * paying for a second model turn — creates.
   *
   * The body comes from the ANSWER, not from the fields applyPlan has just
   * written: those setStates are queued, so `inPlace` and friends still hold
   * the previous opening's values at this point. applyPlan still runs, so that
   * anything which stops the create leaves the user on a form that agrees with
   * the sentence instead of an empty one. */
  const startFromPlan = async (a: PlanAnswer) => {
    // Not an error — the plan is good and the question is the point. The confirm
    // row is already on screen (it derives from the folder applyPlan just
    // wrote), so this only has to say which press comes next.
    const ask = immediateStartBlockReason({
      // `!== true`, the same direction applyPlan reads it: of the two ways to be
      // wrong about a missing key, only one of them makes a directory nobody
      // was shown.
      folderExists: a.folder_exists === true,
      folderLabel: a.folder_display || a.repo_path || "that folder",
      confirmed: newFolderOk,
    });
    if (ask) {
      setPlanError(ask);
      return;
    }
    await postCreate(
      buildBody({
        title: a.title || "",
        repoPath: a.repo_path || "",
        prompt: a.prompt || "",
        inPlace: planInPlace(a),
        initRepo: !!a.init_repo,
        // A plan never provisions — `provisioned` is not in the wire contract
        // and the server never emits it — so an immediate start never does
        // either, whatever the form happened to be holding.
        provisioned: false,
      })
    );
  };

  const runDescribe = async (mode: "fill" | "start" = "fill") => {
    // The in-flight guard comes FIRST, before describeBlockReason is consulted,
    // because that one cannot carry it: it answers "" while a turn is in flight
    // on purpose (the button is already saying "Reading…"). The button is
    // disabled, but the box is readOnly rather than disabled — it keeps focus,
    // so Enter goes on firing, and each press used to buy another
    // POST /api/session-plan and another headless CLI turn. Claiming the slot
    // for a sentence the check below then refuses costs nothing: nothing is in
    // flight to invalidate, and the controller it makes never gets a request.
    const started = startPlanRun(planRun.current, describing);
    if (!started) return;
    const { seq, ctl } = started;
    const blocked = describeBlockReason({ text: describe, busy: describing });
    if (blocked) {
      // Said out loud rather than silently ignored: both the button and Enter
      // come through here, and neither is disabled, so a refusal that showed
      // nothing would read as a dead control.
      setPlanError(blocked);
      return;
    }
    setDescribing(true);
    setDescribeSlow(false);
    setPlanError("");
    // Last run's note described last run's folder. Clearing it up front means
    // the strip never shows a sentence about a form that is being replaced.
    setPlanNote("");
    const slow = window.setTimeout(() => {
      if (planRun.current.seq === seq) setDescribeSlow(true);
    }, DESCRIBE_SLOW_MS);
    try {
      const a = await api<PlanAnswer>("/api/session-plan", {
        json: { text: describe.trim().slice(0, DESCRIBE_MAX_CHARS) },
        signal: ctl.signal,
      });
      if (planRun.current.seq !== seq) return;
      applyPlan(a);
      setPlanNote(a.note || "");
      // Kept so "Start session" can be pressed a second time — after ticking a
      // new folder's confirm — without buying another CLI turn, and without the
      // risk that the second turn answers with a DIFFERENT folder than the one
      // just agreed to.
      lastAnswer.current = { sentence: describe.trim(), answer: a };
      if (mode === "start") {
        // Straight to the create. Nothing below applies: there is no form to
        // hold, no caret to move, and no page 2 to move it on.
        setDescribing(false);
        setDescribeSlow(false);
        void startFromPlan(a);
        return;
      }
      setPage(2);
      // Create is held for a breath before the caret moves, not after: the
      // answer arrives ~10-25s after the keystroke that asked for it, by which
      // time the user's hand may be back on Enter for some other reason.
      submitArmAt.current = Date.now() + SUBMIT_ARM_MS;
      // The Folder field, not Create: it holds the value most likely to be
      // wrong, it is what the note is about, and focusing it scrolls it into
      // view — .nf-body is the scroll region and a filled-in form is taller than
      // the empty one that asked for it, so the folder can easily be above the
      // fold by now. Plain Enter there still submits (that field only swallows
      // Enter for a NAME, and a plan's path is never one), so the two-Enter path
      // survives with the caret on the thing you would be confirming.
      // Deferred to an effect, because the Folder field is on page 2 and page 2
      // has not rendered yet — repoRef.current is still null on this line.
      focusFolderNext.current = true;
    } catch (err) {
      if (planRun.current.seq !== seq) return;
      // Our own cancel, not a failure: cancelDescribe has already put the button
      // back and there is nothing to report.
      if ((err as Error)?.name === "AbortError") return;
      // Inline, never a toast: toast is a 1.4s strip at the bottom of the screen
      // that lands behind this modal, while this is a field-level failure in an
      // open dialog — which is exactly what #new-error and MakePrDialog's .error
      // line already are. The remedy never changes and is already on screen, so
      // the sentence names it.
      setPlanError(errMsg(err) + " — fill in the form below instead.");
    } finally {
      window.clearTimeout(slow);
      // Guarded like every other landing: a later run owns the button now, and
      // an overtaken run must not turn its spinner off.
      if (planRun.current.seq === seq) {
        setDescribing(false);
        setDescribeSlow(false);
        planRun.current.abort = null;
      }
    }
  };

  /** The git nudge's action drives the real "Create a git repo in this folder"
   * checkbox instead of a flag of its own — two switches for one behaviour is
   * how a submitted form ends up disagreeing with what the user was shown. The
   * fold is opened at the same time so the box it just ticked is visible and
   * untickable, rather than changing state out of sight.
   *
   * It pointedly does NOT touch "work directly in this folder" any more. It used
   * to have to: the two boxes disabled each other, so arming one meant clearing
   * the other or leaving the form in a state its own controls forbade. They are
   * combinable now — `git init` here and then work here is the ordinary reading
   * of both boxes together, and the server does exactly that — so silently
   * turning in-place off would be the nudge changing a mode nobody asked it to
   * change. */
  const armInitRepo = () => {
    setInitRepo(true);
    setAdvancedOpen(true);
  };

  if (!open) return null;

  const folderPath = repoPath.trim();
  const offerProvision = provisioningAvailable || !!folderPath;
  // Only an answer about the path now in the field earns a line on screen; a
  // reply for an older path is stale by definition, whether it arrived late or
  // is simply what we last learned before the current keystrokes.
  const plainFolder = folderCheck?.asked === folderPath && !!folderCheck?.plain;
  // The matches that are actually on screen: only for the text now in the field
  // (an answer to an older query is stale by definition), only while the user
  // hasn't dismissed them, and never underneath the folder browser — that is a
  // folder picker too, and two of them stacked under one field, both answering
  // Escape, is one picker too many.
  const searchHits =
    search && search.asked === folderPath && searchOpen && !browserOpen ? search : null;
  // Clamped rather than stored clamped, because the list it indexes into is
  // replaced whole every time an answer lands. -1 when there is nothing to
  // highlight, which is also what tells Enter to fall through to the form.
  const selIndex =
    searchHits && searchHits.matches.length
      ? Math.min(searchSel, searchHits.matches.length - 1)
      : -1;
  // Provisioning and working in place are the two that cannot both be true —
  // provisioning builds a separate worktree or clone, so there is no "this
  // folder" left to work in, and the server enforces exactly that
  // (`in_place = … and not is_provisioned`). Read through offerProvision so a
  // provision box that is not on screen cannot disable a box that is: an
  // in-place checkbox greyed out by an invisible control is unfixable from the
  // dialog.
  const provisionOn = offerProvision && provision;
  // Computed once for both readers: the aside inside the Git & workspace fold,
  // and submit's refusal to post a create the server is certain to refuse.
  const provisionBlocked = provisionBlockReason({
    provision: provisionOn,
    plainFolder,
    initRepo,
    folderPath,
  });
  // Why the selected "New worktree" will not survive the create, or "" when it
  // will. Derived beside provisionBlocked because it is the same kind of fact
  // about the same folder, and because the radio above it is now a positive
  // claim that has to be honest.
  const worktreeClamped = worktreeClampReason({
    inPlace,
    provisionOn,
    plainFolder,
    initRepo,
  });
  // Recomputed from the Folder field on every render rather than remembered, so
  // there is no state to forget to clear: see newFolderGate for the six writers
  // that would each have had to remember. Both readers come off the one value —
  // the confirm row in the Describe strip, and submit's refusal.
  const newFolderAsk = newFolderGate({
    // planGatePath answers the "is it already there" half, so the gate itself
    // only ever compares paths — one place for that question, so the confirm
    // row and submit's refusal cannot come to different conclusions about it.
    planPath: planGatePath(planFolder),
    planDisplay: planFolder.display,
    folderPath,
  });
  const newFolderBlocked = newFolderBlockReason({
    gate: newFolderAsk,
    confirmed: newFolderOk,
  });
  // The note is derived from the same fact as the gate above, and for the same
  // reason: nothing cleared it when the Folder field moved off the plan's path,
  // so a suggestion chip clicked after a `new:` plan left a muted line still
  // saying "Using ~/code/invoice-parser — a new folder" over a form whose
  // field, git nudge and Create had all moved on to somewhere else.
  const planNoteShown = planNoteFor({
    note: planNote,
    planPath: planFolder.path,
    folderPath,
  });
  // Empty groups drop out, so a machine with no recent sessions shows "Nearby"
  // alone instead of two blank label columns.
  const suggestRows = SUGGEST_SOURCES.map((g) => ({
    ...g,
    items: suggestions.filter((s) => s.source === g.key),
  })).filter((g) => g.items.length > 0);

  /** Take one match. The path goes through the reducer like every other way of
   * naming a folder — typing, a chip, the browser, a template — so the git
   * nudge, the chip highlight and Create all follow it, and nothing has to know
   * that this particular folder arrived from a search. The list is shut
   * explicitly rather than left to the effect: the field now holds a path, so
   * the effect will clear the matches anyway, and waiting a render for it to do
   * so leaves the list flashing under the folder it has just filled in. */
  const pickMatch = (path: string) => {
    folderDo({ t: "user-set", path });
    setSearchOpen(false);
  };

  /** Report a create that never happened. The line beside the Create button is
   * the primary surface, but this card scrolls and its actions row is off the
   * bottom of a small window (and a reopened dialog comes back scrolled to the
   * top), so the same words also go to the toast — fixed to the viewport, and
   * impossible to be scrolled away from. */
  const failCreate = (msg: string) => {
    setError(msg);
    toast(msg, { duration: 9000 });
  };

  /** The create body for one session.
   *
   * The six fields a plan decides are PARAMETERS; everything else — agent,
   * launch flags, account, model pin, workspace strategy — is read from the
   * form, where it is either the user's own pick or the default loaded when the
   * dialog opened, and where no plan ever writes. That split is what lets an
   * immediate start build a body from an answer it has just received while the
   * form still holds the previous opening's values. */
  const buildBody = (p: {
    title: string;
    repoPath: string;
    prompt: string;
    inPlace: boolean;
    initRepo: boolean;
    provisioned: boolean;
  }): Record<string, unknown> => {
    const body: Record<string, unknown> = {
      title: p.title.trim(),
      program: program.trim(),
      repo_path: p.repoPath.trim(),
    };
    const promptVal = p.prompt.trim();
    if (promptVal) body.prompt = promptVal;
    // Sent EXPLICITLY (even empty) so a toggled-off default is honored.
    body.launch_args = tokenize(launchArgs);
    // Absent = inherit the app-wide default account (same tri-state as
    // launch_args), so only an explicit pick rides along.
    if (profileId) body.profile_id = profileId;
    if (profileId && profileModel.trim()) body.profile_model = profileModel.trim();
    if (p.provisioned) {
      body.provisioned = true;
      body.workspace_strategy = strategy;
      if (body.repo_path) body.init_repo = p.initRepo;
    } else {
      body.init_repo = p.initRepo;
      body.in_place = p.inPlace;
    }
    return body;
  };

  /** POST the create, and own everything that follows it.
   *
   * BOTH ways of starting a session end here — the form's Create button on page
   * 2, and page 1's "Start session", which never touches the form at all — so
   * the optimistic close, the pending row, the alias fix and the failure reopen
   * happen once and identically. It takes a finished body rather than reading
   * state because that is the only way the second caller can work: applyPlan's
   * setState calls have not been flushed when an immediate start needs the
   * values, so it passes the answer's own fields and this function cannot tell
   * the two apart. */
  const postCreate = async (body: Record<string, unknown>) => {
    setError("Creating…");
    // Close NOW with an optimistic "provisioning" row — the POST can take
    // seconds; on failure the dialog re-opens with fields and error intact.
    const guess = addPendingSession((body.title as string) || "untitled");
    closeDialog();
    try {
      const inst = await api<Instance>("/api/instances", { json: body });
      // The create came back 200, so the plan's folder is on disk now — and the
      // question about making it goes with it. A reopen deliberately KEEPS the
      // plan (see planFolderReducer), so without this the next opening would
      // offer to create a folder that is already there.
      planFolderDo({ t: "created" });
      // Same reason as the guess in addPendingSession: the server's real title
      // must not arrive wearing a closed session's rename.
      clearStaleAlias(inst.title);
      await refreshInstances();
      selectSession(inst.title);
    } catch (err) {
      failPendingSession(guess);
      failCreate((err as Error).message);
      const ui = useUi.getState();
      // Armed only when there is a reopen to arm it for. Someone who reopened
      // the dialog by hand while the POST was in flight is already looking at
      // it, and a flag left set here would go on to suppress the reset of an
      // open that had nothing to do with this failure.
      if (ui.openDialog !== "new-session") {
        failedReopen.current = true;
        // The FORM, not the sentence — including for a create fired from page
        // 1, which is the case that needs it most: a refused create is a thing
        // to fix, every field to fix it with is here, and applyPlan has already
        // put the plan's values in them. Set before the reopen, because the
        // reset effect early-returns on failedReopen and will not touch it.
        setPage(2);
        ui.openDialogFor("new-session");
      }
    }
  };

  const submit = async () => {
    const held = submitHoldReason(submitArmAt.current, Date.now());
    if (held) {
      // First, and — like the guards below — before the optimistic close:
      // once the dialog has shut there is nothing on screen to correct. setError
      // rather than failCreate because a half-second hold is not a create
      // failure and does not deserve a nine-second toast for it.
      setError(held);
      return;
    }
    if (newFolderBlocked) {
      // failCreate, not setError, for the reason its two siblings below give:
      // this card scrolls, its actions row is off the bottom of a small window,
      // and the refusal has to reach somebody whose confirm row is three screens
      // up. And before the optimistic close, like every guard here — once the
      // dialog has shut the directory is made and there is nothing to correct.
      failCreate(newFolderBlocked);
      return;
    }
    if (provisionBlocked) {
      // Before the optimistic close below, for the same reason the name guard
      // is: once the dialog has shut there is nothing on screen to correct.
      failCreate(provisionBlocked);
      return;
    }
    if (isNameQuery(repoPath)) {
      // Every way of reaching Create lands here — the button, Ctrl/Cmd+Enter,
      // and Enter in any other field — so this is the one place that can stop a
      // search term being sent as a folder. It has to stop it BEFORE the
      // optimistic close below: the dialog shuts and the POST goes out in the
      // same breath, so by the time the server has made its stray directory
      // there is nothing left on screen to cancel. See isNameQuery for what the
      // server does with a name.
      failCreate(
        `“${repoPath.trim()}” is a name to look up, not a folder — pick one of the matches, or type a full path starting with / or ~ (Browse… fills one in).`
      );
      return;
    }
    await postCreate(
      buildBody({
        title,
        repoPath,
        prompt,
        inPlace,
        initRepo,
        provisioned: provision,
      })
    );
  };

  const savePreset = () => {
    const text = prompt.trim();
    if (!text) {
      toast("Type a prompt first, then save it as a preset");
      return;
    }
    const name = window.prompt("Preset name:", "");
    if (!name || !name.trim()) return;
    const list = loadUserPresets().filter((p) => p.name !== name.trim());
    list.push({ name: name.trim(), prompt: text });
    saveUserPresets(list);
    setSavedPresets(list);
    setPresetValue("u:" + name.trim());
    toast(`Saved preset “${name.trim()}”`);
  };

  return (
    <div
      id="new-dialog"
      className="modal"
      ref={rootRef}
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          // Escape out of the browser is a cancel, not merely a close: browsing
          // writes each selected row into the Folder field, so leaving it has to
          // hand the field back the way the browser found it.
          if (browserOpen) folderDo({ t: "browse-cancel" });
          else closeDialog();
        } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          submit();
        }
      }}
    >
      <form
        id="new-form"
        // Page 1 is one question and three buttons. The fixed height below
        // exists so that opening a fold on page 2 scrolls inside .nf-body
        // instead of growing the card and re-centering it under the pointer —
        // there are no folds here, and holding 620px up over a single input
        // left most of the card empty.
        className={page === 1 ? "nf-ask" : undefined}
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="ws-head">
          <h2>New session</h2>
          <button type="button" id="new-close" title="Close (Esc)" onClick={closeDialog}>
            Close
          </button>
        </div>

        <div className="nf-body">
          {page === 1 ? (
            <>
          {/* The one-sentence door, first in the body and above Templates
                because it is the widest of the three ways to fill this form in
                (a sentence, a template chip, or by hand) and reads top-down as
                the least specific first. It wears .new-templates so it is the
                same card as the Templates and folder-suggestion strips rather
                than a second form stacked on the first — nothing here is
                submitted, and looking like a form would say otherwise. */}
            <div id="new-describe" className="new-templates nf-describe">
              <div className="nt-head">
                <span>What do you want to work on?</span>
              </div>
              <div className="nf-describe-row">
                <input
                  id="new-describe-text"
                  ref={describeRef}
                  type="text"
                  value={describe}
                  maxLength={DESCRIBE_MAX_CHARS}
                  autoComplete="off"
                  spellCheck={false}
                  // readOnly rather than disabled: a disabled input loses focus to
                  // the body, so the caret would jump out of the box the moment
                  // Enter was pressed and the sentence would stop being
                  // selectable while the user waits to see what it produced.
                  readOnly={describing}
                  placeholder="e.g. fix the login bug in acme-api"
                  onChange={(e) => {
                    setDescribe(e.target.value);
                    // Typing is what clears the refusal: whatever it objected to
                    // has just changed, and a stale red line under a box the user
                    // is actively fixing is noise.
                    setPlanError("");
                  }}
                  onKeyDown={(e) => {
                    if (e.key !== "Enter" || e.ctrlKey || e.metaKey) return;
                    // Enter here means "read this", never "create a session". This
                    // input sits inside <form id="new-form" onSubmit={submit}>, so
                    // without the preventDefault a sentence nobody has resolved
                    // yet becomes a create in whatever folder the suggestion
                    // pre-fill happened to leave behind. stopPropagation keeps the
                    // modal root's Ctrl+Enter handler out of it.
                    e.preventDefault();
                    e.stopPropagation();
                    void runDescribe("fill");
                  }}
                />
                {describing && (
                  <button
                    type="button"
                    id="new-describe-cancel"
                    className="linklike"
                    onClick={cancelDescribe}
                  >
                    Cancel
                  </button>
                )}
              </div>
              {/* Written for somebody who has never opened this dialog. The
                  line this replaced — "Enter fills in the form — or start the
                  session straight away" — named two things a first-time user
                  has no way to know: WHICH form, and what starting it straight
                  away skips. This says what the sentence is for, and that
                  reading it costs nothing, which is the fact that makes the
                  button row below safe to experiment with. */}
              <p className="nf-describe-help">
                Your coding CLI reads this and works out which folder to use, what
                to call the session, and what to tell the agent first.{" "}
                <b>Nothing is created until you pick one of the buttons below.</b>
              </p>
              {/* Two examples rather than none: the placeholder can only show
                  one shape and the box accepts three — an existing project, a
                  brand-new one, and a request for somewhere separate to work. A
                  user who cannot tell which of those is allowed types the safest
                  thing they can think of and never finds the other two. */}
              <p className="nf-describe-eg">
                Also understood:{" "}
                <code>start a new project called invoice-parser</code>
                {" · "}
                <code>add metrics to billing, in a worktree</code>
              </p>
              {planNoteShown && (
                <p className="nf-describe-note" aria-live="polite">
                  {planNoteShown}
                </p>
              )}
              {planError && (
                <p id="new-describe-error" className="error" aria-live="polite">
                  {planError}
                </p>
              )}
              {newFolderAsk && (
                /* The one control in this dialog whose "no" is the safe answer, so
                   it is drawn as a question and not as a fourth checkbox: a folder
                   is the only thing a plan proposes that survives the session, and
                   an option row reads as something you may skim past. It sits at
                   the FOOT of the strip — below the note that explains the folder
                   and below the error, which is about the sentence rather than
                   about this — so the eye meets it on its way down to the form it
                   is holding up.
  
                   It is announced as well as the note above it. Two polite regions
                   firing together is a little chatty; a gate a screen reader never
                   mentioned, on a form whose Create then refuses, is worse. */
                <div
                  className="nf-newfolder"
                  role="group"
                  aria-labelledby="new-describe-newfolder-q"
                >
                  <p id="new-describe-newfolder-q" className="nf-newfolder-q" aria-live="polite">
                    There is no folder at <b>{newFolderAsk}</b> yet. Make it?
                  </p>
                  <label className="check">
                    <input
                      type="checkbox"
                      id="new-describe-newfolder"
                      checked={newFolderOk}
                      onChange={(e) => setNewFolderOk(e.target.checked)}
                    />
                    {/* Word for word what newFolderBlockReason quotes, because the
                        refusal is read several screens below this row and has to
                        name a control the user can go and find. */}
                    Yes, create {newFolderAsk}{" "}
                    <span className="muted">
                      — a new directory, made when you press Create. Not the same as “Create a git
                      repo in this folder” under Git &amp; workspace, which runs git init inside it;
                      a new project usually wants both.
                    </span>
                  </label>
                </div>
              )}
            </div>
            </>
          ) : (
            <>
          {templates.length > 0 && (
              <div id="new-templates" className="new-templates">
                <div className="nt-head">
                  <span>Templates</span>
                  <button
                    type="button"
                    id="new-templates-manage"
                    className="linklike"
                    onClick={() => {
                      const w = window as unknown as {
                        mindflockAddons?: { templates?: { open?: () => void } };
                      };
                      const t = w.mindflockAddons?.templates;
                      if (t && typeof t.open === "function") {
                        closeDialog();
                        t.open();
                      } else toast("Templates manager isn't loaded");
                    }}
                  >
                    Manage…
                  </button>
                </div>
                <div id="new-templates-list" className="nt-list">
                  {templates.map((t) => (
                    <button
                      key={t.name}
                      type="button"
                      className={"nt-chip" + (activeTemplate === t.name ? " active" : "")}
                      data-name={t.name}
                      title={(t.program ? "[" + t.program + "] " : "") + (t.prompt || "launch this recipe")}
                      onClick={() => fillFromTemplate(t)}
                    >
                      {t.name}
                    </button>
                  ))}
                </div>
              </div>
            )}
  
            <label>
              <span>
                Name <span className="muted">— optional; empty starts an untitled session</span>
              </span>
              <input
                id="new-title"
                ref={titleRef}
                autoComplete="off"
                placeholder="untitled"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>
  
            <div className="nf-quick">
              <label
                className="nf-folder"
                title="Any folder works — git features (diff / commit / PR) turn on automatically when it's a git repo."
              >
                Folder
                <span className="repo-path-row">
                  <input
                    id="new-repo-path"
                    ref={repoRef}
                    autoComplete="off"
                    placeholder="/home/me/projects/foo — or a folder name to look up"
                    value={repoPath}
                    // Announced as a combobox because it is one now: without these
                    // a screen reader hears an ordinary text box, the arrow keys
                    // move a highlight nothing reports, and Enter fills the field
                    // from a list that was never mentioned.
                    role="combobox"
                    aria-expanded={!!searchHits}
                    aria-controls="new-search-list"
                    aria-autocomplete="list"
                    aria-activedescendant={selIndex >= 0 ? "new-search-hit-" + selIndex : undefined}
                    onChange={(e) => {
                      folderDo({ t: "user-set", path: e.target.value });
                      // Typing is what brings a dismissed list back: the query has
                      // changed, so the reason it was dismissed went with it.
                      setSearchOpen(true);
                    }}
                    onKeyDown={(e) => {
                      const hits = searchHits?.matches || [];
                      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                        if (!searchHits && search && search.asked === folderPath && !browserOpen) {
                          // Matches exist for exactly this text and were merely
                          // dismissed: the first arrow brings them back, rather
                          // than making the user type a character and delete it
                          // again to see the list they just closed.
                          e.preventDefault();
                          setSearchOpen(true);
                          return;
                        }
                        if (!hits.length) return;
                        // Otherwise the arrows would take the caret to the ends of
                        // the path instead of moving the highlight.
                        e.preventDefault();
                        const step = e.key === "ArrowDown" ? 1 : -1;
                        const at = Math.min(searchSel, hits.length - 1) + step;
                        setSearchSel(Math.max(0, Math.min(hits.length - 1, at)));
                      } else if (
                        e.key === "Enter" &&
                        !e.ctrlKey &&
                        !e.metaKey &&
                        isNameQuery(folderPath)
                      ) {
                        // This input sits inside the form whose submit CREATES the
                        // session, so a plain Enter must not leak out of a field
                        // that is holding a NAME — with or without matches under
                        // it. With a highlight it takes the highlighted folder;
                        // preventDefault then stops the session being created in a
                        // folder the user had only just chosen, before they had
                        // seen the choice land. Without one — the search found
                        // nothing, or has not answered yet, or the list was
                        // dismissed with Escape — it does nothing, which is the
                        // whole point: gating this branch on the matches meant
                        // Enter on "notathing" submitted the SEARCH TERM as
                        // repo_path, and the server resolves a bare name against
                        // its own working directory and creates it. A search that
                        // came up empty must not be one keystroke from a session in
                        // a stray folder next to the server.
                        //
                        // Ctrl/Cmd+Enter is excluded on purpose: that chord means
                        // "create now" everywhere else in the dialog, the modal's
                        // own handler owns it, and submit() refuses a name there.
                        // A field holding a real path keeps plain Enter as submit,
                        // exactly as it behaved before this field could search.
                        e.preventDefault();
                        if (hits.length) pickMatch(hits[Math.min(searchSel, hits.length - 1)].path);
                        else if (search && search.asked === folderPath && !browserOpen)
                          // Matches exist and were merely dismissed: Enter brings
                          // them back rather than swallowing the keystroke, the
                          // same courtesy the arrows do above.
                          setSearchOpen(true);
                      } else if (e.key === "Escape" && searchHits) {
                        // Escape dismisses the list and nothing else: not the typed
                        // text, which is the query and the one thing the user would
                        // resent retyping, and not the dialog — so it must not
                        // reach the modal's Escape handler above.
                        e.preventDefault();
                        e.stopPropagation();
                        setSearchOpen(false);
                      }
                    }}
                  />
                  <button
                    type="button"
                    id="repo-browse-btn"
                    title={
                      browserOpen
                        ? "Close the browser and keep this folder (Esc puts the old one back)"
                        : "Browse local folders"
                    }
                    onClick={() =>
                      // Toggling the panel shut keeps whatever the field now holds:
                      // it is on screen, the user has been looking at it, and only
                      // Escape claims to undo anything.
                      folderDo(
                        browserOpen ? { t: "browse-commit", path: repoPath } : { t: "browse-open" }
                      )
                    }
                  >
                    Browse…
                  </button>
                </span>
              </label>
              <label className="nf-agent">
                <span className="nf-agent-head">
                  Agent
                  <button
                    type="button"
                    id="new-agent-manage"
                    className="linklike"
                    title="Manage coding CLIs in Settings"
                    onClick={() => {
                      closeDialog();
                      useUi.getState().openDialogFor("settings", "coding");
                    }}
                  >
                    Manage
                  </button>
                </span>
                <select
                  id="new-program"
                  title="The coding CLI this session runs"
                  value={program}
                  onChange={(e) => setAgent(e.target.value)}
                >
                  {!providers.some((p) => p.name === program) && program && (
                    <option value={program}>{program}</option>
                  )}
                  {providers.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              {(authProfiles?.profiles || []).length > 0 && (
                <label className="nf-agent">
                  <span className="nf-agent-head">
                    Account
                    <button
                      type="button"
                      id="new-account-manage"
                      className="linklike"
                      title="Manage accounts in Settings"
                      onClick={() => {
                        closeDialog();
                        useUi.getState().openDialogFor("settings", "accounts");
                      }}
                    >
                      Manage
                    </button>
                  </span>
                  <select
                    id="new-account"
                    title="Which identity this session's CLI runs as"
                    value={profileId}
                    onChange={(e) => setAccount(e.target.value)}
                  >
                    <option value="">
                      {authProfiles?.default_profile
                        ? `App default (${authProfiles.default_profile})`
                        : "App default (CLI's own login)"}
                    </option>
                    <option value="default">CLI's own login</option>
                    {(authProfiles?.profiles || []).map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.label || p.id}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
  
            {selectedProfile && selectedProfile.kind !== "account" && (
              <div className="nf-quick">
                <label className="nf-agent" style={{ flex: 1 }}>
                  <span className="nf-agent-head">Model</span>
                  {(profileModels[profileId] || []).length ? (
                    <select
                      id="new-account-model"
                      title="Model this session runs on (through the selected account)"
                      value={profileModel}
                      onChange={(e) => setProfileModel(e.target.value)}
                    >
                      <option value="">
                        {selectedProfile.model
                          ? `Account default (${selectedProfile.model})`
                          : "Account default"}
                      </option>
                      {/* A value typed before the catalog landed (or absent from
                          it) stays VISIBLE and selected — coercing the display
                          to "Account default" while still submitting it would
                          launch a model the form no longer shows. */}
                      {profileModel &&
                        !(profileModels[profileId] || []).includes(profileModel) && (
                          <option value={profileModel}>{profileModel} (custom)</option>
                        )}
                      {(profileModels[profileId] || []).map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      id="new-account-model"
                      type="text"
                      autoComplete="off"
                      placeholder={
                        selectedProfile.model
                          ? `Account default (${selectedProfile.model})`
                          : "anthropic/claude-sonnet-4.5"
                      }
                      value={profileModel}
                      onChange={(e) => setProfileModel(e.target.value)}
                    />
                  )}
                </label>
              </div>
            )}
  
            {routeWarning && <p className="nf-git-nudge">{routeWarning}</p>}
            {/* The datalist mount slots.js populates from /api/providers. */}
            <datalist id="provider-list"></datalist>
  
            {searchHits && (
              /* The name-search results. Wears .new-templates and .nf-suggest for
                 the same reason the suggestion strip below does — same card, same
                 head, same pills, a different source — and adds only what a search
                 hit needs that a suggestion chip doesn't: its path. Two folders
                 called `api` are told apart by where they live, and telling them
                 apart is the entire point of having searched. */
              <div id="new-search" className="new-templates nf-suggest nf-search">
                <div className="nt-head">
                  <span>Matches for “{searchHits.asked}”</span>
                  <span className="nf-suggest-legend">↑↓ choose · Enter fills · Esc closes</span>
                </div>
                {searchHits.matches.length > 0 && (
                  <div
                    id="new-search-list"
                    className="nf-search-list"
                    ref={searchListRef}
                    role="listbox"
                    aria-label="Folder matches"
                  >
                    {searchHits.matches.map((m, i) => (
                      <button
                        key={m.path}
                        id={"new-search-hit-" + i}
                        type="button"
                        role="option"
                        aria-selected={i === selIndex}
                        /* .active is the suggestion strip's "this is the folder
                           you get" treatment — accent border and tint, light
                           theme included — and that is precisely what the
                           highlight means here, so it reuses it rather than
                           inventing a second way to say the same thing. */
                        className={
                          "nt-chip" + (m.is_git ? " is-git" : "") + (i === selIndex ? " active" : "")
                        }
                        data-path={m.path}
                        title={m.path + (m.is_git ? "" : "\nno git repo here yet")}
                        // Hovering moves the highlight so the mouse and the
                        // keyboard never disagree about which row Enter takes.
                        onMouseMove={() => setSearchSel(i)}
                        onClick={() => pickMatch(m.path)}
                      >
                        <span className="nf-search-name">{(m.is_git ? "📦 " : "📁 ") + m.name}</span>
                        <span className="nf-search-path">
                          {homeRelative(m.path, searchHits.home || homePath)}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
                {searchHits.matches.length === 0 && (
                  /* An empty result has to name the ways out, because the field
                     looks identical whether the search found nothing or was never
                     a search at all. Both sentences are written to claim only what
                     the walk can actually support. A budget that tripped before
                     finding anything did not look everywhere, so "not here" would
                     be a claim it never got far enough to make — and it does not
                     ask the user to type more of the name, because the walk is
                     query-independent (the needle only RANKS what was already
                     reached) and a longer query re-walks the same directories for
                     the same budget. Nor does the complete-walk sentence say
                     "nothing under your home directory is called X": the search
                     stops at three levels, never enters a git repo and skips
                     hidden and node_modules-shaped folders, so a `widget` inside
                     the monorepo the user lives in is plainly under home and
                     plainly not something this walk can see. */
                  <p className="nf-search-empty muted">
                    {searchHits.truncated ? (
                      <>
                        The search stopped at its time and size limit before it found “
                        {searchHits.asked}” — Browse… walks straight to it, and a path starting with /
                        or ~ is used exactly as typed.
                      </>
                    ) : (
                      <>
                        No folder called “{searchHits.asked}” in the first three levels under your
                        home directory — the search doesn’t look inside git repos or hidden folders.
                        Browse… reaches those and the rest of the disk, and a path starting with / or
                        ~ is used exactly as typed.
                      </>
                    )}
                  </p>
                )}
                {searchHits.matches.length > 0 && searchHits.truncated && (
                  /* Deliberately does NOT say "more folders matched": truncation
                     is one flag over three causes (the scan cap, the 1.5s deadline
                     and the row limit), and under either of the first two no extra
                     match is known to exist — the walk simply stopped. "May not be
                     everything" is true of all three. */
                  <p className="nf-search-note muted">
                    The search stopped early, so this list may not be everything — Browse… if the
                    folder you want isn’t here.
                  </p>
                )}
              </div>
            )}
  
            {plainFolder && (
              <p className="nf-git-nudge">
                {initRepo ? (
                  <>A git repo will be created here — diff, commit and PR will work.</>
                ) : (
                  <>
                    No git repo in this folder, so diff, commit and PR stay off.{" "}
                    <button
                      type="button"
                      className="linklike"
                      title="Ticks “Create a git repo in this folder” under Git & workspace"
                      onClick={armInitRepo}
                    >
                      Create one
                    </button>
                  </>
                )}
              </p>
            )}
  
            {suggestRows.length > 0 && (
              /* Wears .new-templates as well as its own class on purpose: this is
                 the same chip strip as Templates, filled from a different source,
                 and sharing the class is what stops the two from drifting apart. */
              <div id="new-suggest" className="new-templates nf-suggest">
                <div className="nt-head">
                  <span>Folders</span>
                  <span className="nf-suggest-legend">📦 git repo · 📁 plain folder</span>
                </div>
                {suggestRows.map((g) => (
                  <div key={g.key} className="nf-suggest-row">
                    <span className="nf-suggest-label" title={g.hint}>
                      {g.label}
                    </span>
                    <FitRow>
                      {g.items.map((s) => {
                        const active = folderPath === s.path;
                        return (
                          <button
                            key={s.path}
                            type="button"
                            className={
                              "nt-chip" + (s.is_git ? " is-git" : "") + (active ? " active" : "")
                            }
                            data-path={s.path}
                            aria-pressed={active}
                            title={s.path + (s.is_git ? "" : "\nno git repo here yet")}
                            onClick={() => folderDo({ t: "user-set", path: s.path })}
                          >
                            {(s.is_git ? "📦 " : "📁 ") + s.name}
                          </button>
                        );
                      })}
                    </FitRow>
                  </div>
                ))}
              </div>
            )}
  
            {browserOpen && (
              <FolderBrowser
                initialPath={folderPath || homePath || ""}
                selected={folderPath}
                onSelect={(p) => folderDo({ t: "browse-select", path: p })}
                onPick={(p) => folderDo({ t: "browse-commit", path: p })}
              />
            )}
  
            <details
              id="new-advanced"
              className="nf-advanced"
              data-caps="git"
              open={advancedOpen}
              onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
            >
              <summary>
                Git &amp; workspace
              </summary>
              <div className="nf-advanced-body">
                {/* ONE question with three answers, not a radio pair plus a
                    stray checkbox.
  
                    "Isn't Provision workspace the same exact thing as New
                    worktree?" — asked about the previous shape, and a fair
                    reading of it: both do produce a separate checkout, they sat
                    in the same fold, and nothing said how they were related. They
                    are not the same (provisioning ALSO runs the repo's setup
                    commands and seeds the warm caches, and can make a full clone
                    instead of a worktree), but "worktree" being a radio while
                    "provision" was a checkbox implied they answered DIFFERENT
                    questions, when they answer the same one: where does this
                    session's work happen.
  
                    Three radios say the relationship out loud — provisioning is
                    the worktree option plus setup — and cost nothing in
                    behaviour: the three states were already mutually exclusive,
                    each old setter clearing the other by hand. This is that
                    exclusion written down instead of maintained. */}
                <div
                  className="nf-mode"
                  role="radiogroup"
                  aria-labelledby="new-mode-label"
                >
                  <div id="new-mode-label" className="nf-mode-label">
                    Where the work happens
                  </div>
                  <label className="check">
                    <input
                      type="radio"
                      name="new-workspace-mode"
                      id="new-worktree"
                      checked={!inPlace && !provisionOn}
                      onChange={() => {
                        setInPlace(false);
                        setProvision(false);
                      }}
                    />
                    New worktree{" "}
                    <span className="muted">
                      (a separate checkout on its own branch — nothing is installed
                      into it)
                    </span>
                  </label>
                  <label className="check">
                    <input
                      type="radio"
                      name="new-workspace-mode"
                      id="new-in-place"
                      checked={inPlace}
                      onChange={() => {
                        setInPlace(true);
                        setProvision(false);
                      }}
                    />
                    Work directly in this folder{" "}
                    <span className="muted">
                      (no worktree — edits the original; multiple sessions can share
                      it)
                    </span>
                  </label>
                  {offerProvision && (
                    <label id="new-provision-row" className="check">
                      <input
                        type="radio"
                        name="new-workspace-mode"
                        id="new-provision"
                        checked={provisionOn}
                        onChange={() => {
                          setProvision(true);
                          setInPlace(false);
                        }}
                      />
                      Provision workspace{" "}
                      <span className="muted">
                        — the same separate checkout, plus run repo setup &amp; warm
                        test caches (or a full clone instead of a worktree)
                      </span>
                    </label>
                  )}
                  {worktreeClamped && (
                    /* The server would silently do this anyway; saying so is the
                       difference between a form that reports the session you are
                       about to get and one that reports the session you asked
                       for. Same register and same remedy as the git nudge under
                       the Folder field. */
                    <p className="nf-git-nudge">{worktreeClamped}</p>
                  )}
                  {provisionBlocked && (
                    /* The git aside under the Folder field says the same thing in
                       the register of a plain session, where a repo-less folder is
                       merely a folder without diff/commit/PR. Here it is a hard
                       stop, it is three screens further down, and the fix is the
                       checkbox directly below — so it earns its own line. */
                    <p className="nf-git-nudge nf-provision-warn">
                      {provisionBlocked}{" "}
                      <button
                        type="button"
                        className="linklike"
                        title="Ticks “Create a git repo in this folder” below"
                        onClick={armInitRepo}
                      >
                        Create one here
                      </button>
                    </p>
                  )}
                  {offerProvision && provision && (
                    <div id="provision-opts">
                      <label>
                        Workspace strategy
                        <select
                          id="new-workspace-strategy"
                          value={strategy}
                          onChange={(e) => setStrategy(e.target.value)}
                        >
                          <option value="worktree">shared base clone (worktree) — fast, default</option>
                          <option value="clone">full clone — standalone</option>
                        </select>
                      </label>
                      <p className="muted provision-hint">
                        Tip: paste a full branch in <b>Name</b> (e.g.{" "}
                        <code>feature/sc-17436/grafana-dashboard-…</code>) to use it as the branch
                        verbatim — the session name becomes its last segment.
                      </p>
                    </div>
                  )}
                </div>
  
                {/* OUTSIDE the mode group, because it is not a fourth answer to
                    "where does the work happen" — it is a thing done to the folder
                    before any of the three run, and it combines with all of them.
                    No longer exclusive with "work directly in this folder", and
                    the pairing was always backwards: git-initialising a folder and
                    then working in that same folder is the ordinary thing to want
                    — arguably the most natural mode for a folder you just made,
                    since a worktree cut from a brand-new repo is the awkward case.
                    The server does exactly that combination: _prepare_plain_repo
                    git-inits and makes the first commit, and the session comes up
                    in place with diff/commit/PR on. */}
                <label className="check">
                  <input
                    type="checkbox"
                    id="new-init-repo"
                    checked={initRepo}
                    onChange={(e) => setInitRepo(e.target.checked)}
                  />
                  Create a git repo in this folder{" "}
                  <span className="muted">(git init + initial commit — enables diff/commit/PR)</span>
                </label>
              </div>
            </details>
  
            <details
              id="new-prompt-fold"
              className="nf-advanced"
              ref={promptRef}
              open={promptOpen}
              onToggle={(e) => {
                const open = (e.target as HTMLDetailsElement).open;
                setPromptOpen(open);
                // Scroll to it only when a PERSON opened it. A plan opens this
                // fold to show the prompt it wrote, and scrolling to the bottom
                // of the form on arrival buries the two fields most likely to
                // be wrong — see foldOpenedByPlan.
                if (foldOpenedByPlan.current) {
                  foldOpenedByPlan.current = false;
                  return;
                }
                if (open)
                  promptRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
              }}
            >
              <summary>
                Prompt <span className="muted">— sent to the agent at launch</span>
              </summary>
              <div className="nf-advanced-body">
                <label>
                  <span className="preset-row">
                    <select
                      id="new-preset"
                      title="Prompt presets — pick one to fill the prompt below (editable after)"
                      value={presetValue}
                      onChange={(e) => {
                        setPresetValue(e.target.value);
                        const p = findPreset(e.target.value);
                        if (p) setPrompt(p.prompt);
                      }}
                    >
                      <option value="">Preset…</option>
                      {BUILTIN_PRESETS.length > 0 && (
                        <optgroup label="Built-in">
                          {BUILTIN_PRESETS.map((p) => (
                            <option key={"b:" + p.name} value={"b:" + p.name} title={p.prompt}>
                              {p.name}
                            </option>
                          ))}
                        </optgroup>
                      )}
                      {savedPresets.length > 0 && (
                        <optgroup label="Saved">
                          {savedPresets.map((p) => (
                            <option key={"u:" + p.name} value={"u:" + p.name} title={p.prompt}>
                              {p.name}
                            </option>
                          ))}
                        </optgroup>
                      )}
                    </select>
                    <button type="button" id="preset-save" title="Save current prompt as preset…" onClick={savePreset}>
                      Save…
                    </button>
                    {presetValue.startsWith("u:") && (
                      <button
                        type="button"
                        id="preset-del"
                        title="Delete the selected saved preset"
                        onClick={() => {
                          const p = findPreset(presetValue);
                          if (!p) return;
                          const list = loadUserPresets().filter((q) => q.name !== p.name);
                          saveUserPresets(list);
                          setSavedPresets(list);
                          setPresetValue("");
                        }}
                      >
                        ✕
                      </button>
                    )}
                  </span>
                  <textarea
                    id="new-prompt"
                    rows={2}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="What should the agent do first? Leave blank if you don’t want to kick anything off just yet."
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                  />
                </label>
              </div>
            </details>
  
            <details
              id="new-launch-advanced"
              className="nf-advanced"
              ref={launchRef}
              open={launchOpen}
              onToggle={(e) => {
                const open = (e.target as HTMLDetailsElement).open;
                setLaunchOpen(open);
                // The fold is the last thing in the scroll region, so its revealed
                // fields open below the fold line — scroll them into view so it's
                // obvious the click did something and where to look. Only on a
                // real click: open-by-default fires no toggle, and scrolling the
                // body on arrival would bury the Name field.
                if (open)
                  launchRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
              }}
            >
              <summary>
                Launch flags <span className="muted">— extra CLI flags for this session</span>
              </summary>
              <div className="nf-advanced-body">
                <label>
                  <span>
                    Flags{" "}
                    <span className="muted">
                      — e.g. --dangerously-skip-permissions; appended after the agent's saved defaults
                    </span>
                  </span>
                  <input
                    type="text"
                    id="new-launch-args"
                    autoComplete="off"
                    placeholder="--dangerously-skip-permissions"
                    value={launchArgs}
                    onChange={(e) => setLaunchArgs(e.target.value)}
                  />
                </label>
                <FlagChips provider={program} value={launchArgs} onChange={setLaunchArgs} />
              </div>
            </details>
            </>
          )}
        </div>
        <div className="modal-actions nf-actions">
          <p id="new-error" className="error">{error}</p>
          {page === 1 ? (
            <>
              {/* THREE actions, in increasing order of commitment, left to
                  right — and the row has to say which is which on its own,
                  because the previous labels ("Skip — fill it in myself",
                  "✨ Fill in the form", "Start session now") were all written
                  from inside the app's own vocabulary. "The form" is page 2,
                  which nobody has seen yet; "skip" does not say what is being
                  skipped or what you get instead. These name the OUTCOME.

                  The escape is first, quiet, and pushed to the far left by its
                  own margin so it reads as "not one of these two". It cannot be
                  dropped: page 1 is where every opening lands, so this is the
                  only route to the form for someone who does not want to
                  describe anything — or who has no coding CLI installed to
                  describe it to. */}
              <button
                type="button"
                id="new-describe-skip"
                className="nf-quiet"
                title="Go straight to the full form and choose the folder and options yourself. Nothing is read, and no model runs."
                onClick={() => setPage(2)}
              >
                Set it up myself instead
              </button>
              <button
                // type="button" is load-bearing, not tidiness: the default is
                // "submit", so without it this button creates a session out of
                // whatever the form happens to be holding.
                type="button"
                id="new-describe-go"
                // Disabled only while a turn is in flight — NEVER for a sentence
                // that is too short. A greyed-out control that will not say why
                // is furniture explaining itself; both this and Enter go through
                // describeBlockReason instead and get the sentence.
                disabled={describing}
                aria-busy={describing || undefined}
                title="Work out the folder, name and first instruction, then show them to you so you can change anything before the session is created."
                onClick={() => void runDescribe("fill")}
              >
                {describing ? (
                  <>
                    {/* The ring AND a changed label: a cold CLI start plus a
                        real turn runs to ~25s, and a button that only spins
                        reads as a hang long before the server's own timeout
                        would say anything. */}
                    <span className="btn-spin" aria-hidden="true" />{" "}
                    {describeSlow ? "Still reading…" : "Reading…"}
                  </>
                ) : (
                  <>
                    Review details first{" "}
                    {/* What Enter does, shown rather than described. The
                        sentence that used to say it in words sat in the header
                        and explained the wrong page. */}
                    <span className="nf-key" aria-hidden="true">
                      ↵
                    </span>
                  </>
                )}
              </button>
              {/* The "don't make me read anything" path. Accent, because it is
                  the one this page exists for, and a BUTTON rather than the
                  Enter key on purpose: Enter in the box means "read this", it
                  has meant that since the box existed, and a key that creates a
                  session in a folder nobody has looked at is the one gesture
                  this whole feature has been careful not to build. The single
                  exception it does not skip is a folder that does not exist —
                  see immediateStartBlockReason. */}
              <button
                type="button"
                id="new-describe-start"
                disabled={describing}
                title="Create the session right now from what you typed, without showing you the details first."
                onClick={startNow}
              >
                Create session
              </button>
            </>
          ) : (
            <>
              {/* Back, not Cancel: page 1 still holds the sentence, and a
                  create that has not happened yet is not something to undo. */}
              <button
                type="button"
                id="new-back"
                className="linklike"
                onClick={() => setPage(1)}
              >
                ← Back
              </button>
              <button type="submit">Create</button>
            </>
          )}
        </div>
      </form>
    </div>
  );
}

/** Which row a keyboard navigation should land on once the new listing is up:
 * the folder it just stepped out of, if that folder is in the listing — which is
 * the case when stepping UP, so a wrong turn is one ← away from where it started
 * — and otherwise the first row, since a folder is never among its own children.
 * -1 means the listing has no rows to land on at all. */
export function focusRowIndex(paths: string[], leaving: string): number {
  if (!paths.length) return -1;
  const i = paths.indexOf(leaving);
  return i >= 0 ? i : 0;
}

/** Folder browser popover (port of loadBrowse, section 16), on Finder's rules:
 * a single click SELECTS a row and a double click opens it. It used to be the
 * other way round — the name navigated and a per-row "select" button was the
 * only way to choose anything — which meant the one gesture everybody tries
 * first did the one thing they didn't ask for. Selecting deliberately does NOT
 * close the popover: the user is mid-browse, and a picker that vanishes on the
 * first click is unusable for comparing two folders.
 *
 * Selecting writes into the Folder field itself rather than keeping a copy in
 * here, so the highlight, the git nudge and the suggestion chips all follow the
 * row that was clicked. That is what makes the dialog's snapshot load-bearing:
 * see folderReducer for how Escape hands the field back. */
function FolderBrowser({
  initialPath,
  selected,
  onSelect,
  onPick,
}: {
  initialPath: string;
  /** The path the Folder field holds, so the highlight always matches the form
   * rather than a copy of the selection kept in here. */
  selected: string;
  /** Fill the field, stay open — undone if the browse is cancelled. */
  onSelect(path: string): void;
  /** Fill the field and close — the explicit "I'm done" gesture. */
  onPick(path: string): void;
}) {
  interface BrowsePayload {
    path: string;
    parent?: string | null;
    is_git?: boolean;
    entries?: Array<{ name: string; path: string; is_git?: boolean }>;
  }
  const [data, setData] = useState<BrowsePayload | null>(null);
  const [error, setError] = useState("");
  const listRef = useRef<HTMLDivElement | null>(null);
  // The folder a keyboard navigation is stepping out of, or null when the
  // pointer drove it (or nothing is pending) — read by the effect below.
  const leaving = useRef<string | null>(null);

  const load = useCallback(async (path: string) => {
    setError("");
    try {
      const q = path ? "?path=" + encodeURIComponent(path) : "";
      setData(await api<BrowsePayload>("/api/browse" + q));
    } catch (err) {
      // A listing that never arrived leaves the old rows (and the row that has
      // focus) on screen, so it must also drop any pending handoff rather than
      // leave it to fire on somebody else's navigation.
      leaving.current = null;
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load(initialPath);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Navigating swaps every row in the list, which unmounts the button holding
  // focus — and focus falls to document.body, so → and ← worked exactly once and
  // getting back into the list meant Tabbing in from the top of the document,
  // past the whole modal. So a keyboard move claims a row in the NEW listing
  // (see focusRowIndex), and an empty folder hands focus to "use this folder",
  // which down there is the only thing left to do anyway. Pointer navigation
  // names nothing and so moves nothing: the mouse has not lost its place.
  useEffect(() => {
    const from = leaving.current;
    if (from === null || !data) return;
    leaving.current = null;
    const idx = focusRowIndex((data.entries || []).map((e) => e.path), from);
    const rows = listRef.current?.querySelectorAll<HTMLButtonElement>(".rb-item:not(.rb-use)");
    const row =
      idx >= 0 ? rows?.[idx] : listRef.current?.querySelector<HTMLButtonElement>(".rb-use");
    row?.focus();
  }, [data]);

  /** Navigate the way the keyboard has to: name the folder being left before the
   * rows are replaced, so the new listing knows where to put focus. */
  const keyLoad = (to: string) => {
    leaving.current = data?.path || "";
    load(to);
  };

  const mkdir = async () => {
    if (!data?.path) return;
    const name = window.prompt("New folder name (created in " + data.path + "):", "");
    if (!name || !name.trim()) return;
    setError("");
    try {
      const r = await api<{ path: string }>("/api/mkdir", {
        json: { path: data.path, name: name.trim() },
      });
      // Naming a brand-new folder in a prompt IS the deliberate choice, so this
      // stays a pick: field filled, browser closed. (Nothing re-lists afterwards
      // for that reason — the popover is already gone.)
      onPick(r.path);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div id="repo-browser">
      <div className="rb-head">
        {/* ← rather than ↑: every file browser people already use — Finder,
            Explorer, a web page — spells "out of here" as back, and the row
            list reads as a place you stepped into, not a level you climbed. */}
        <button
          type="button"
          id="rb-up"
          title="Parent folder"
          aria-label="Parent folder"
          disabled={!data?.parent}
          onClick={() => data?.parent && load(data.parent)}
        >
          ←
        </button>
        <span id="rb-cwd" className="rb-cwd" title={data?.path || ""}>
          {data?.path || ""}
        </span>
        <button type="button" id="rb-mkdir" title="Create a new folder here" onClick={mkdir}>
          + Folder
        </button>
      </div>
      <div id="rb-list" ref={listRef}>
        {data && (
          /* Names the folder it means. Child rows are selectable now, so "use
             this folder" on its own would be ambiguous about which folder — the
             one you're standing in, or the one you just highlighted. */
          <button
            type="button"
            className={
              "rb-item rb-use" +
              (data.is_git ? " is-git" : "") +
              (selected === data.path ? " rb-sel" : "")
            }
            title={"Use " + data.path + " and close the browser"}
            onClick={() => onPick(data.path)}
          >
            {(data.is_git ? "✓ use this repo — " : "use this folder — ") + leafName(data.path)}
          </button>
        )}
        {(data?.entries || []).map((e) => (
          // Rows are real buttons: they're the primary control now, so keyboard
          // reach, Enter/Space activation and a focus ring should come from the
          // platform rather than a div wearing tabIndex and a key handler.
          // Selecting is idempotent, which is what makes the double click safe —
          // its first click writes the same path the second one would, so there
          // is no second visible effect to notice.
          <button
            key={e.path}
            type="button"
            className={
              "rb-item" + (e.is_git ? " is-git" : "") + (selected === e.path ? " rb-sel" : "")
            }
            aria-pressed={selected === e.path}
            title={e.path}
            onClick={() => onSelect(e.path)}
            onDoubleClick={() => {
              onSelect(e.path);
              load(e.path);
            }}
            onKeyDown={(ev) => {
              // → is the keyboard's double click and ← is its ↑ button: a
              // pointer has both gestures, and Shift+Tabbing out of a long list
              // to reach the parent button is not a substitute.
              if (ev.key === "ArrowRight") {
                ev.preventDefault();
                onSelect(e.path);
                keyLoad(e.path);
              } else if (ev.key === "ArrowLeft" && data?.parent) {
                ev.preventDefault();
                keyLoad(data.parent);
              }
            }}
          >
            <span className="rb-name">{(e.is_git ? "📦 " : "📁 ") + e.name}</span>
          </button>
        ))}
      </div>
      {/* "cancels", not "closes": Esc puts the folder the field started with
          back, and a legend that said otherwise is what made passing through the
          wrong tree cost anything. The Browse button's tooltip spells the pair
          out; this line has to stay one line, since #repo-browser pays for its
          height out of the list. */}
      <p className="rb-hint">Click selects · double-click or → opens · ← up · Esc cancels</p>
      <p id="rb-error" className="error">{error}</p>
    </div>
  );
}
