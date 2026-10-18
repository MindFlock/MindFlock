/** The pure half of NewSessionDialog.tsx, the way verify.ts is VerifyDialog's:
 * the Folder field's reducer, the plan's folder reducer and the gates every
 * route to Create consults. No React here, so each ordering bug these were
 * written against can be replayed in a test. */

import type { PlanAnswer } from "../../api/types";

/** The two things New makes. Session is the landing tab and stays that way:
 * it is the hot path (name → Enter), and a tab strip that makes the common
 * case pay a click of attention has already cost more than it gave. */
export type NewTab = "session" | "ticket";

export const NEW_TABS: Array<{ key: NewTab; label: string }> = [
  { key: "session", label: "Session" },
  { key: "ticket", label: "Ticket" },
];

export interface Provider {
  name: string;
  aliases?: string[];
  command?: string;
  /** The CLI can declare a plan the Map shows with a Go button (Claude).
   * Absent on older servers, which offered Plan first for every CLI — so
   * only an explicit `false` hides it. */
  plan_supported?: boolean;
}

/** One folder /api/repos/suggest thinks the user might mean. */
export interface RepoSuggestion {
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
export const SUGGEST_SOURCES: Array<{ key: string; label: string; hint: string }> = [
  { key: "recent", label: "Recent", hint: "folders your recent sessions ran in" },
  { key: "cwd", label: "Started in", hint: "the folder MindFlock itself was started in" },
  { key: "nearby", label: "Home", hint: "repos and folders sitting under your home directory" },
];

/** How long the Folder field must sit still before we ask the server what it
 * is. The endpoint answers 200-with-exists:false for half-typed paths, so a
 * per-keystroke call would be harmless but pointless traffic. */
export const CHECK_DEBOUNCE_MS = 400;

/** How long the Folder field must sit still before a NAME typed into it is
 * looked up. Deliberately half the path probe's wait above, because the two
 * debounces buy different things: the check only decorates a field the user has
 * already finished with, while the match list is the thing they are sitting
 * there waiting to read, and 400ms of blank space reads as "searching doesn't
 * work here". The walk behind it is bounded server-side (see
 * ``search_repos``'s scan cap and 1.5s deadline), so asking a keystroke early
 * costs a bounded amount. */
export const SEARCH_DEBOUNCE_MS = 200;

/** Below this a query is not worth a walk: one character matches most of the
 * machine and tells the user nothing. ``search_repos`` enforces the same floor
 * and answers an empty list rather than an error, so this copy only saves the
 * round trip. */
export const SEARCH_MIN_CHARS = 2;

/** Below this the Describe box is not worth a model turn. The floor is about
 * ambiguity, not length: one word names no project and no task, and the answer
 * to "scan" is a folder the model guessed and the user has to undo. Twenty-five
 * seconds to be told nothing useful is the worst trade this feature can make. */
export const DESCRIBE_MIN_CHARS = 8;

/** What the box will send, matching the server's own MAX_SENTENCE. A pasted
 * paragraph is truncated rather than refused — a brief is still a request. */
export const DESCRIBE_MAX_CHARS = 2000;

/** How long the ring runs before the label admits it. The generator's budget is
 * 75s and a cold CLI start plus a real turn is ~10s, so a spinner that never
 * changes reads as a hang long before the timeout does. */
export const DESCRIBE_SLOW_MS = 8000;

/** How long Create is held after a fill lands. Focus moves when the answer
 * arrives, ~10-25s after the user pressed Enter — long enough that their hand
 * may be back on Enter for a reason that has nothing to do with this form.
 * Long enough to catch that, far short of the time it takes to read a note. */
export const SUBMIT_ARM_MS = 500;

/** The New dialog's one starting verb (GLOSSARY "Start"): page 1's primary,
 * the form's submit, and every sentence that names either. */
export const START_LABEL = "Start";

/** The first sentence of the PR tooltip (lib/sessionActions PR_FALLBACK_HINT):
 * why Fast-track's Open PR / Merge rungs can't be picked while GitHub is not
 * connected — the same words the session menu uses. */
export function prNeedsGithubReason(fallbackHint: string): string {
  return fallbackHint.split("\n")[0];
}

/** "Advanced"'s fold summary: the names of what is actually inside it, in
 * order, so it never promises a control that isn't there. */
export function advancedSummary(parts: Array<[string, boolean]>): string {
  return parts
    .filter(([, shown]) => shown)
    .map(([name]) => name)
    .join(", ");
}

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

/** Which children of the one-line folder row to hide (true = hidden), given
 * each child's right edge as laid out with nothing hidden.
 *
 * Everything fits: nothing hides and the row needs no "N more…" chip. Else
 * room is kept for that chip (`moreWidth`, its gap included) and every child
 * past the shortened edge hides — in a no-wrap row hiding a later child never
 * moves an earlier one, so one pass settles it. Then a group label left with
 * no visible chip after it hides too: "Home" alone at the end of the row is a
 * heading over nothing. */
export function foldChipRow(
  kids: ReadonlyArray<{ label: boolean; right: number }>,
  rowRight: number,
  moreWidth: number
): boolean[] {
  if (kids.every((k) => fitsWithin(k.right, rowRight))) return kids.map(() => false);
  const hidden = kids.map((k) => !fitsWithin(k.right, rowRight - moreWidth));
  for (let i = 0; i < kids.length; i++) {
    if (!kids[i].label || hidden[i]) continue;
    let j = i + 1;
    while (j < kids.length && !kids[j].label && hidden[j]) j++;
    if (j === kids.length || kids[j].label) hidden[i] = true;
  }
  return hidden;
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
/** Last path segment, for naming the folder the confirm row would use. Keeps
 * both separators so a Windows path doesn't come back as the whole string. */
export function leafName(path: string): string {
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
  | { t: "browse-cancel" }
  /** The session will live on another device: a path from this one's disk
   * (or the last device's) names nothing over there. Start blank, so the
   * new device's own suggestion fills it. */
  | { t: "device" };

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
    case "device":
      return { ...FOLDER_INIT };
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
    " does not exist yet. Press \u201CCreate this folder\u201D, then " +
    START_LABEL +
    " again."
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
    "\u2014 pick \u201CMake it a git repo first\u201D to get a real worktree."
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
    "No folder at " +
    where.gate +
    " — press “Create this folder” under Folder, or pick a folder that already exists."
  );
}

/** The shut "Git & workspace" fold's summary: where the work happens and,
 * for a new worktree, what it is cut from — so the choice is read without a
 * click. Pure, so the sentence is pinned by tests. */
export function foldSummaryFor(where: {
  provisioned: boolean;
  strategy: string;
  inPlace: boolean;
  baseBranch: string;
  current: string;
  initRepo: boolean;
  /** "Work on x itself" was picked for a remote-tracking base. */
  continueBranch?: boolean;
}): string {
  let s: string;
  if (where.provisioned)
    s = "provisioned " + (where.strategy === "clone" ? "clone" : "worktree");
  else if (where.inPlace) s = "in this folder";
  else if (where.continueBranch && remoteBranchName(where.baseBranch))
    s = "worktree on " + remoteBranchName(where.baseBranch) + " (shared branch)";
  else {
    const base = where.baseBranch || where.current;
    s = "new worktree" + (base ? " from " + base : "");
  }
  if (where.initRepo) s += " · git init";
  return s;
}

/** "origin/feature/x" → "feature/x": the branch a remote-tracking name is
 * for ("" when it carries no remote prefix). */
export function remoteBranchName(ref: string): string {
  const m = /^(?:[^/\s]+)\/(.+)$/.exec((ref || "").trim());
  return m ? m[1] : "";
}

/** The Base branch pick as create-body fields. A remote-tracking pick
 * ("origin/x") either cuts a NEW branch from it, compared against "x"
 * (`base_ref` + `base_branch`) — the default — or, with "Work on x itself",
 * puts the session ON "x" tracking "origin/x" (`base_ref` + `continue_branch`;
 * the server validates and refuses one checked out elsewhere). A local pick
 * is always a new branch. Pure, so the contract is pinned by tests. */
export function baseBranchFields(
  base: string,
  remote: boolean,
  continueBranch: boolean
): Record<string, string> {
  base = (base || "").trim();
  if (!base) return {};
  const name = remote ? remoteBranchName(base) : "";
  if (name && continueBranch) return { base_ref: base, continue_branch: name };
  return { base_ref: base, base_branch: name || base };
}

/** What the opening's pre-fill does with the account the last session in
 * this folder used: "apply" it when the current profiles still have it (or
 * it is "default", the CLI's own login), "drop" it from memory when the list
 * has loaded without it (the account was removed — posting it 400s "unknown
 * account" on every Name → Enter), "wait" while the list hasn't loaded. */
export function rememberedProfileVerdict(
  id: string,
  profiles: { id: string }[] | null | undefined
): "apply" | "drop" | "wait" | "none" {
  if (!id) return "none";
  if (id === "default") return "apply";
  if (!profiles) return "wait";
  return profiles.some((p) => p.id === id) ? "apply" : "drop";
}

/** What the form's Name → Enter does, and what page 1's sentence box's Enter
 * does. Pure, so both are pinned by a test: Enter in Name submits the form
 * (one POST /api/instances, no model turn); Enter in the sentence box is
 * Start — the primary, which starts it now in the folder the line under the
 * box names (the folder is on screen there as a picker, so nothing starts
 * unseen); Shift+Enter (and any Enter in a batch's list) is a new line.
 * "Let the agent set it up…" — the model turn — is a click, never a key. */
export function enterAction(where: {
  field: "name" | "describe" | "other";
  shift: boolean;
  runMode: boolean;
}): "create" | "start" | "newline" | "none" {
  if (where.field === "describe") return where.shift || where.runMode ? "newline" : "start";
  if (where.shift) return "none";
  return "create";
}

/** What Ctrl/Cmd+Enter does anywhere in the New dialog's Session tab.
 *
 * On page 1 (the sentence card) the form is not on screen, and submitting it
 * once created an "untitled" session with the sentence thrown away. So on
 * page 1 the shortcut is Start, exactly like Enter: the sentence as the task,
 * in the folder the line under the box shows. A list (run mode) keeps
 * starting its rows, as its own Start button does. Page 2 submits the form
 * the user is looking at. */
export function ctrlEnterAction(where: { page: number; runMode: boolean }): "start" | "submit" {
  if (where.page === 1 && !where.runMode) return "start";
  return "submit";
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

// --- Round 2: defaults, previews and guards -----------------------------------

/** The describe box gives up after this long and offers the form instead,
 * pre-filled with what was typed. The server's own budget is the same 30s
 * (session_plan.TIMEOUT_PLAN); a cold CLI start plus a real turn is ~10s. */
export const DESCRIBE_CAP_MS = 30000;

/** Words that say nothing about a task — the server's _TITLE_FILLER. */
const TITLE_FILLER = new Set(
  (
    "a an the to of in on at for and or with by from into please can could " +
    "you we i my our your this that it is be some"
  ).split(" ")
);
export const TITLE_FROM_PROMPT_MAX = 32;

/** The name a session with an empty Name gets from its task: the mirror of
 * the server's session_create.title_from_prompt ("Add a README section about
 * setup" → "add-readme-section-about-setup"). "" when the task has no
 * letters or digits — the server then calls it "untitled". */
export function titleFromPrompt(prompt: string): string {
  const first = (prompt || "").split(/\r?\n/).find((l) => l.trim()) || "";
  const words = first.toLowerCase().match(/[a-z0-9]+/g) || [];
  const kept = words.filter((w) => !TITLE_FILLER.has(w));
  const use = kept.length ? kept : words;
  let out = "";
  for (const w of use.slice(0, 6)) {
    const next = out ? out + "-" + w : w;
    if (next.length > TITLE_FROM_PROMPT_MAX) break;
    out = next;
  }
  if (!out && use.length) out = use[0].slice(0, TITLE_FROM_PROMPT_MAX);
  return out;
}

/** git-branch-safe, as the engine's sanitize_branch_name builds it. */
export function sanitizeBranch(s: string): string {
  return s
    .toLowerCase()
    .replace(/ /g, "-")
    .replace(/[^a-z0-9\-_/.]/g, "")
    .replace(/-+/g, "-")
    .replace(/^[-/]+|[-/]+$/g, "");
}

/** "/a/b/" and "/a/b" are one folder; "~" is left as typed. */
function samePath(a: string, b: string): boolean {
  const n = (p: string) => (p || "").trim().replace(/\/+$/, "");
  return !!n(a) && n(a) === n(b);
}

/** The sessions already editing `paths` (the folder as typed, and as the
 * server resolved it) DIRECTLY — the ones a second in-place session would
 * share a checkout with. */
export function sessionsInFolder(
  rows: Array<{ title: string; path: string; in_place?: boolean }>,
  paths: string[]
): string[] {
  return rows
    .filter((r) => r.in_place && paths.some((p) => samePath(r.path, p)))
    .map((r) => r.title);
}

/** The warning under "This folder directly" when others already edit it. */
export function sharedCheckoutWarning(titles: string[]): string {
  if (!titles.length) return "";
  const who =
    titles.length === 1 ? "“" + titles[0] + "” is" : titles.length + " sessions are";
  return (
    who +
    " already editing this folder directly. Pick “Separate worktree” so they " +
    "don't overwrite each other."
  );
}

/** `base`, or `base-2`, `base-3`… — the first name not in `taken`. The
 * server's session_create.free_title (and _free_untitled for "untitled"),
 * so the preview names the branch Create will really make. */
export function freeTitle(base: string, taken: ReadonlySet<string>): string {
  let title = base;
  for (let n = 2; taken.has(title); n++) title = base + "-" + n;
  return title;
}

/** What Create will call the session and its branch, given the names
 * already in use. `clash` is a TYPED name another session already has (the
 * server refuses it); `renumbered` is the name made from the task that was
 * taken, so this one gets a number. */
export interface PlannedName {
  title: string;
  branch: string;
  /** Where the name comes from: typed, made from the task, or neither. */
  from: "name" | "task" | "none";
  clash: boolean;
  renumbered: string;
}

export function plannedName(where: {
  prefix: string;
  title: string;
  prompt: string;
  taken: ReadonlySet<string>;
}): PlannedName {
  const typed = where.title.trim();
  if (typed)
    return {
      title: typed,
      branch: sanitizeBranch((where.prefix || "") + typed),
      from: "name",
      clash: where.taken.has(typed),
      renumbered: "",
    };
  const base = titleFromPrompt(where.prompt);
  const from = base ? "task" : "none";
  const title = freeTitle(base || "untitled", where.taken);
  return {
    title,
    branch: sanitizeBranch((where.prefix || "") + title),
    from,
    clash: false,
    renumbered: title !== (base || "untitled") ? base || "untitled" : "",
  };
}

/** The names a new session can't have: every session's title and the name
 * it shows (a rename), as the server's free_title checks them. Another
 * device's rows (`device::title`) live in that device's namespace. */
export function takenNames(
  rows: Array<{ title: string }>,
  aliases: Record<string, string>
): Set<string> {
  const out = new Set<string>();
  for (const r of rows) {
    if (!r.title || r.title.includes("::")) continue;
    out.add(r.title);
    const a = aliases[r.title];
    if (a) out.add(a);
  }
  return out;
}

/** The sessions already working in this repo, however they work in it
 * (their own worktree or the folder itself). Verify's checklist runs and
 * another device's rows are not "someone working here". */
export function sessionsInRepo(
  rows: Array<{ title: string; path: string; hidden_by?: string }>,
  paths: string[]
): string[] {
  return rows
    .filter(
      (r) => !r.hidden_by && !r.title.includes("::") && paths.some((p) => samePath(r.path, p))
    )
    .map((r) => r.title);
}

/** The one line under "Works in" when other sessions already work in this
 * repo — the answer to "will a second one clobber the first?". */
export function otherSessionsLine(titles: string[], worktree: boolean): string {
  if (!titles.length) return "";
  const who =
    titles.length === 1
      ? "“" + titles[0] + "” is"
      : titles.length + " other sessions are";
  return (
    who +
    " already working in this repo" +
    (worktree ? " — a separate worktree keeps you apart." : ".")
  );
}

/** What GET /api/providers/status says about each agent on this device:
 * name → installed. Absent = unknown (another device, an older server). */
export type InstalledMap = Record<string, boolean>;

/** Why Create must not launch `agent` ("" = go ahead). Only a KNOWN miss
 * blocks: an agent nobody asked about may well be there. */
export function agentMissingReason(agent: string, installed: InstalledMap | null): string {
  if (!installed || !agent) return "";
  if (installed[agent] !== false) return "";
  return (
    agent +
    " isn't installed on this device. Install it in Settings → Agents, or pick another agent."
  );
}

/** The agent the form should start on: the configured default, unless that
 * one is known to be missing and another is installed. */
export function preferInstalled(agent: string, names: string[], installed: InstalledMap | null): string {
  if (!installed || installed[agent] !== false) return agent;
  return names.find((n) => installed[n] === true) || agent;
}

/** Why `name` can't be saved as a NEW template: one of `existing` already has
 * it, ignoring case — the server upserts by name, so saving would replace
 * that one, schedule and all. `editing` is the template being edited, whose
 * own name is of course fine. "" = free. */
export function templateNameTaken(
  name: string,
  existing: Array<{ name: string }>,
  editing: string | null = null
): string {
  const want = name.trim().toLowerCase();
  if (!want || (editing && editing.trim().toLowerCase() === want)) return "";
  const hit = existing.find((t) => t.name.trim().toLowerCase() === want);
  return hit ? `A template named “${hit.name}” already exists — pick another name.` : "";
}

/** The fields a template carries, from the form — the WHOLE setup, so Run now
 * and a schedule start the same session the form would have. A template of
 * the same name keeps its schedule (saving over it must not silently stop a
 * nightly run). Pure, so what is saved is pinned by tests. */
export function templateFromForm(
  f: {
    name: string;
    program: string;
    repoPath: string;
    prompt: string;
    inPlace: boolean;
    provision: boolean;
    strategy: string;
    initRepo: boolean;
    lane: string;
    askFirst: boolean;
  },
  existing: Array<{ name: string; schedule?: unknown }>
): Record<string, unknown> {
  const name = f.name.trim();
  const prior = existing.find((t) => t.name.trim().toLowerCase() === name.toLowerCase());
  const lane = f.lane && f.lane !== "leave" ? f.lane : "";
  return {
    name,
    program: f.program.trim(),
    repo_path: f.repoPath.trim(),
    prompt: f.prompt,
    provisioned: f.provision,
    in_place: f.inPlace && !f.provision && !lane,
    workspace_strategy: f.strategy === "clone" ? "clone" : "worktree",
    init_repo: f.initRepo,
    // A template carries its run as far as an autopilot depth (commit / push
    // / pr): it never merges unattended, so the New form's "merge" saves as "pr".
    ...(lane ? { autopilot: lane === "merge" ? "pr" : lane } : {}),
    ...(prior?.schedule ? { schedule: prior.schedule } : {}),
  };
}

/** "0:07" for an elapsed count of seconds. */
export function elapsedLabel(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/** Set by the Templates dialog when it hands the person back to New (its
 * Close, after New → Manage…): the reopen keeps the form as it was instead of
 * resetting it, and only reloads the template list. One-shot. `prompt` is a
 * prompt picked in Prompts (opened from New's Task box) for the Task box. */
export const newSessionResume: { keep: boolean; prompt?: string } = { keep: false };

/** Whether the New dialog holds something the person TYPED, which a
 * reopen keeps (closing New, a trip to Intake or Settings) until Create or
 * Clear. A Task the opening's own last-used pre-fill wrote is not typed. */
export function hasTypedDraft(d: {
  describe: string;
  title: string;
  prompt: string;
  prefilledPrompt: string;
}): boolean {
  if (d.describe.trim() || d.title.trim()) return true;
  const p = d.prompt.trim();
  return !!p && p !== d.prefilledPrompt.trim();
}

/** What an opening does with the form: a failed create's own reopen and a
 * hand-back from Templates/Prompts keep it exactly; Intake's "Start
 * together…" (a prefill) always starts fresh; otherwise a typed draft is
 * kept unless the last close was a Create. */
export function openingMode(o: {
  failedReopen: boolean;
  resume: boolean;
  prefill: boolean;
  draft: boolean;
  spent: boolean;
}): "failed" | "resume" | "fresh" {
  if (o.failedReopen) return "failed";
  if (o.prefill) return "fresh";
  if (o.resume) return "resume";
  if (o.draft && !o.spent) return "resume";
  return "fresh";
}
