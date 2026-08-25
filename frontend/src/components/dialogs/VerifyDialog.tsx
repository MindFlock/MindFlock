/** The Verify dialog — what shipped, and does it work?
 *
 * This is deliberately NOT a fifth Intake tab, even though it borrows Intake's
 * whole visual vocabulary. Intake is the FRONT of the pipeline: every tab there
 * answers "what work is out there, and which of it may become a session?", and
 * every row's action is *start something*. Verify is the BACK of it. Its rows
 * are not candidates, they are things that already happened — a branch that has
 * reached the live branch — and its action is *check something*. Putting them
 * on one strip would have meant a tab whose count means "waiting to begin" next
 * to a tab whose count means "waiting to be believed", and a user reading the
 * two as one queue would be reading it wrong.
 *
 * There is a second, blunter reason: Intake is where you go when you want more
 * work, and this is where you go when you are done with a piece of it. Nobody
 * opens "the intake screen" to close the loop on last Tuesday's merge. It gets
 * its own top-bar entry, its own badge and its own key (Alt+V) because the
 * moment it exists to serve — a plan going due while you were somewhere else —
 * is exactly the moment nobody remembers to go looking.
 *
 * THE SHAPE, and why it is this one. The surface is one grouped queue, and the
 * first group IS the badge: `verify.ts` exports one predicate, `dueCount`
 * filters by it, and the "Waiting on you" heading renders exactly the plans it
 * counted. There is no tab strip, because the tab strip was a filter that
 * rendered the top of the list under the same headings — one number printed in
 * four places, and no two of them ever equal.
 *
 * Every row carries exactly ONE status: a plain-English sentence saying whose
 * turn it is, and one button that does what the sentence says. It replaced four
 * chips that routinely disagreed (a plan could read "checked" next to "partial"
 * under a heading that said you had to confirm it) and four paragraphs of body
 * prose that nobody expanded a row to read. `planStatus` decides the sentence,
 * the tone and the button together, from the same `planGroup` the heading came
 * from, so the three cannot contradict each other.
 *
 * Everything rare — rewriting, deleting, running something that has not shipped
 * — lives in a ⋯ menu.
 *
 * THE ANATOMY IS INTAKE'S, in Intake's order: an intro sentence, the master
 * switch, the SOURCES it watches, the work those sources produced, and a
 * `<details>` at the bottom for what you touch once. PullRequestsTab and
 * IssuesTab are laid out exactly this way, down to `.set-block-hint` and
 * `.pr-advanced`, and Verify is the fourth surface of the same kind — it should
 * not have to be learned separately. The switch is `repository.verify_enabled`;
 * it pauses the automatic half only (no checklist written on a push, no liveness
 * pass moving work into the list) and deliberately leaves writing one by hand,
 * running and answering a step working, exactly as a forced PR review still runs
 * with automated review switched off.
 *
 * ONE NOUN AND ONE VERB, throughout. The feature is **Verify**, the artifact is
 * a **checklist**, and the act is to **check**. "Test plan" survives in the API
 * paths, the store, the settings keys and these docstrings, where no user reads
 * it — on screen it was a fifth name for a thing that already had four, and to
 * a developer it reads as pytest.
 *
 * The honest words are visible, not hovered. "Blocked is never a pass", who
 * recorded an answer and when, how to take one back, and the two one-way doors
 * (running a plan before it ships, or hand-answering a pre-live one, both close
 * it permanently) were all in `title` attributes — on the one surface whose
 * entire claim is that its labels are true, and where keyboard and touch users
 * never saw them.
 *
 * The furniture (`ws-head`, `pr-open-chip`, `repo-empty`, `btn-primary`,
 * WorkListPanel / WorkGroup) is Intake's, reused rather than re-cut: the two
 * surfaces are read minutes apart by the same person, and a second dialect of
 * "a list of work with counts on it" would be a tell that one of them is bolted
 * on. What is genuinely new here — the plan row, the step row and the overflow
 * menu — lives in VerifyDialog.css under `vf-`.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  TestPlan,
  TestPlansResponse,
  TestStep,
  TestStepActor,
  TestStepResult,
  TestStepResultEntry,
} from "../../api/types";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import {
  queryClient,
  refreshInstances,
  refreshTestPlans,
  useInstances,
  useTestPlans,
} from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
// FAILURES DO NOT GO IN A TOAST. `toast()` is a 1.4-second confirmation strip at
// the bottom of the screen; the failures this surface produces are a paragraph
// of git or tmux output whose remedy is its last sentence ("… is already used by
// worktree at <path>"), and they were being shown in a strip that clipped them
// and vanished before they could be read — from a MODAL, so the strip was often
// behind it. Intake already puts failures in the bottom-right cards that stay
// until dismissed; this surface now uses the same corner for the same reason.
import { errorPop } from "../../lib/errorPop";
import { errMsg } from "../../lib/format";
// THE LIST DIALOGS' OWN KIT, not a second one. Recently-closed and Workspaces
// already have a Ctrl+F filter and checkbox multi-select; a checklist list that
// grows without bound needs both for the same reason, and inventing a Verify
// dialect of either would make the third list the one people have to learn.
import { searchTokens } from "../../lib/rowSearch";
import { previewList } from "../../lib/rowSelection";
import { DialogFilter } from "./DialogFilter";
import {
  BulkRowBar,
  RowCheck,
  SelectAllCheck,
  useRowSelection,
  type RowSelection,
} from "./rowSelect";
import {
  AutomationSwitch,
  WorkGroup,
  WorkListPanel,
  ageText,
  panelNote,
  useToggleSet,
} from "../intake/kit";
import { RepoSourceList, type RepoOverrides } from "../intake/RepoSources";
import { SettingsCtx, useSettings, useSettingsModel } from "../settings/useSettings";
import { useWsTerm } from "../../lib/wsTerm";
import {
  CHECK_MARK,
  asksHumanSteps,
  type CheckState,
  type ClosedTarget,
  checkTally,
  closedTargets,
  failCount,
  groupPlans,
  handedBack,
  isWaitingOnYou,
  isYourAnswer,
  latestRun,
  liveBranchOverridden,
  noTargetsReason,
  openHumanSteps,
  canRunNow,
  planMatches,
  planStatus,
  planTargets,
  rewriteBlockedReason,
  rewriteWarning,
  runEvidence,
  runTreeMismatch,
  noteDraftAfter,
  stepKeyAction,
  stepKeyAllowed,
  stepKeyIsUndo,
  stepCheck,
  stepIsYours,
  stepResult,
  tallyBits,
  tallySentence,
} from "./verify";

/** Per-device UI state, same reasoning as Intake's group toggles (kit.tsx):
 * "I collapsed Checked on this laptop" is not a property of the flock.
 *
 * Both sets store the EXCEPTIONS, so a fresh install writes nothing at all.
 * Groups default OPEN and plans default CLOSED, and each set holds whatever was
 * toggled away from that — one polarity per set, unlike the arrangement this
 * replaced, where a plan's saved gesture was XOR'd against whether its group was
 * urgent, so a plan you deliberately collapsed re-opened itself the moment it
 * changed group. The keys and the stored format are unchanged, so nobody's saved
 * state resets; a legacy entry simply reads as "I opened this". */
const GROUPS_KEY = "mf_verify_groups";
const PLANS_KEY = "mf_verify_plans";

/** `/api/test-plans/{id:path}` — the id is a session title and may contain a
 * slash. encodeURIComponent is still right (and is what instApi does for the
 * same reason): the ASGI server percent-decodes the path before routing, so a
 * `%2F` arrives at the path converter as the slash it was. */
function planPath(id: string): string {
  return "/api/test-plans/" + encodeURIComponent(id);
}

/** A plan's human label. `title` is what the generator was told the work was
 * called; `id` is the session title it was keyed by, and is never empty. */
function planName(plan: TestPlan): string {
  return plan.title || plan.id;
}

/** kit.ts's ageText speaks ISO strings (every Intake payload is JSON from an
 * upstream API); the plan store keeps epoch seconds. One conversion here rather
 * than a second age formatter, so "3h old" means the same thing on both
 * surfaces down to the rounding. */
function ageOf(epochSeconds: number): string {
  if (!epochSeconds) return "";
  return ageText(new Date(epochSeconds * 1000).toISOString());
}

/** The same age phrased as a MOMENT rather than as a duration: "3h ago".
 *
 * "live, 3h old" is how you describe a thing's age; "shipped 3h ago" is how you
 * describe when something happened, and every timestamp on a plan row is the
 * latter. One suffix swap rather than a second formatter, so the two spellings
 * can never round differently. */
function agoOf(epochSeconds: number): string {
  const age = ageOf(epochSeconds);
  return age ? age.replace(/ old$/, " ago") : "";
}

/** What each answer is CALLED, everywhere a person reads one.
 *
 * The wire value stays `blocked` — it is the store's word, the run prompt's
 * word and the agent's word, and changing it would rewrite every recorded
 * answer for a relabel. What changes is the four places a person meets it: the
 * button, the button's tooltip, the receipt beside it, and the legend.
 *
 * All four, from one map, because relabelling only the button is worse than
 * relabelling none of it — pressing "Can't check" and watching "blocked · you"
 * appear one flexbox gap away teaches the reader that the surface's words are
 * not the system's words, on the one surface whose entire claim is that its
 * labels are true.
 *
 * "Can't check" rather than "Blocked" because it says what it means without a
 * legend, and because "blocked" is jargon that collides with the agent's own
 * use of it ("blocked · agent" = not mine to answer) — two meanings, one word,
 * on the same row. */
const ANSWER_LABEL: Record<string, string> = {
  pass: "Pass",
  fail: "Fail",
  blocked: "Can't check",
};

/** What each check glyph means, for the tooltip and the screen reader. The
 * glyphs themselves live in `verify.ts` beside the states they render, so the
 * roll-up and the rows cannot end up using different marks for one state. */
const CHECK_LABEL: Record<CheckState, string> = {
  pass: "Passed",
  fail: "Failed",
  cant: "You couldn't check this",
  yours: "Waiting on you",
  pending: "Not checked yet",
};

function answerLabel(result: TestStepResult): string {
  return ANSWER_LABEL[result] || result;
}

/** What a RECORDED answer is called, which depends on who recorded it.
 *
 * The button says "Can't check" because a person pressing it is saying they
 * could not get to the thing. An AGENT's `blocked` is a different sentence — it
 * is declining to answer, because no shell can observe what the step asks about
 * — so reading the button's words back over the agent's answer would put the
 * user's meaning in its mouth and make "can't check · agent" look like a result
 * somebody had already given. */
function receiptLabel(entry: TestStepResultEntry): string {
  if (entry.result === "blocked" && entry.by !== "human") return "for you to check";
  return answerLabel(entry.result).toLowerCase();
}

/** Who settled a step, in the second person. The store writes "human" for
 * anything a person recorded, including through this dialog. */
function byLabel(by: string): string {
  return by === "human" ? "you" : by || "agent";
}

/** Swap one plan in the cached list without waiting for the 10s poll.
 *
 * Every mutating route answers with the state the server just wrote, and the
 * list is shared with the top bar's badge — so the response is applied to the
 * cache directly and `refreshTestPlans()` follows only as a reconcile. Without
 * this, pressing Pass left the row unchanged for up to ten seconds, which reads
 * as "the click didn't take" and gets clicked again. */
function patchPlan(id: string, apply: (plan: TestPlan) => TestPlan) {
  // CANCEL THE POLL THAT IS ALREADY OPEN. `useTestPlans` refetches every 10s and
  // query-core resolves a successful fetch with an unconditional `setData` — it
  // has no idea a manual update landed while it was in flight — so an optimistic
  // row could be overwritten by a GET that left before the POST. Every other
  // mutating path here escapes only by accident: they follow the patch with
  // `refreshTestPlans()`, whose `invalidateQueries` defaults to
  // `cancelRefetch: true` and silently cancels the stale request. Rewrite
  // deliberately does not refetch (the plan is mid-generation), so it was the
  // one that snapped back — to a row offering "Run with an agent" on a plan the
  // model was still writing. Unawaited on purpose: the cancel is synchronous
  // where it matters, and nothing here needs its promise.
  void queryClient.cancelQueries({ queryKey: ["test-plans"] });
  queryClient.setQueryData<TestPlansResponse>(["test-plans"], (prev) =>
    prev ? { ...prev, plans: prev.plans.map((p) => (p.id === id ? apply(p) : p)) } : prev
  );
}

/** One entry in a ⋯ menu. */
interface MenuItem {
  key: string;
  label: string;
  title?: string;
  /** Turns red on hover. The confirm() is the real guard; this is the warning. */
  danger?: boolean;
  /** Extra classes for an item some test pins by name (see "Cancel run"). */
  className?: string;
  onSelect(): void;
}

/** The ⋯ overflow: every action that applies but is not the one you came for.
 *
 * A native `<details>` rather than a hand-rolled popover, because the open state
 * then survives without a portal and the summary is focusable and announced for
 * free. What is NOT free is the three things a menu owes its user, all of which
 * are here: Escape closes it (in the capture phase, so the first Escape closes
 * the MENU and only the second closes the dialog behind it), a click anywhere
 * else closes it, and it flips upward near the bottom of the scroll container
 * rather than opening into a clipped strip nobody can read.
 *
 * Renders nothing at all when there is nothing applicable — a ⋯ that opens onto
 * an empty box is worse than no ⋯. */
function OverflowMenu({ label, items }: { label: string; items: MenuItem[] }) {
  const ref = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useState(false);
  const [up, setUp] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      // Capture phase + stopPropagation: the dialog's own Escape listener sits
      // on document in the bubble phase, so without this one keypress would
      // close the menu and the whole dialog together.
      e.stopPropagation();
      e.preventDefault();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  if (!items.length) return null;

  return (
    <details className={"vf-menu" + (up ? " vf-menu-up" : "")} open={open} ref={ref}>
      <summary
        aria-label={label}
        title={label}
        onClick={(e) => {
          // The <details> would toggle itself; we drive it so the flip can be
          // measured on the way open, while the row is still where it was.
          e.preventDefault();
          const next = !open;
          if (next && ref.current) {
            const here = ref.current.getBoundingClientRect();
            const body = document.getElementById("verify-body");
            const floor = body ? body.getBoundingClientRect().bottom : window.innerHeight;
            setUp(floor - here.bottom < 24 * items.length + 40);
          }
          setOpen(next);
        }}
      >
        ⋯
      </summary>
      <div className="vf-menu-list">
        {items.map((item) => (
          <button
            key={item.key}
            type="button"
            className={
              "vf-menu-item" +
              (item.danger ? " vf-menu-danger" : "") +
              (item.className ? " " + item.className : "")
            }
            title={item.title}
            onClick={() => {
              setOpen(false);
              item.onSelect();
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
    </details>
  );
}

/** One step of an expanded plan: what to do, what to expect, who it is for, and
 * the three answers a person can give it.
 *
 * The three answers come FIRST and sit in fixed grid tracks, so ten steps are
 * one aiming task rather than ten. They used to follow an elastic note input,
 * which put the common act a third of the way across every row at an x that
 * changed with the length of the step text.
 *
 * `pending` is optimism without invention: while the POST is in flight the row
 * renders the answer you just gave, but nothing fabricates a *run* in the
 * cache. A plan that has never been run has no run object to write into, and
 * inventing one client-side would put "agent" next to results no agent
 * produced. The server's copy of the plan arrives a few milliseconds later and
 * is what the row settles on. */
/** Note composers that survive a row being remounted, keyed plan+step.
 *
 * WHY THIS IS NOT REACT STATE. Answering your last open step moves the plan to
 * another group, and `PlanList` renders each group's rows under its own
 * `WorkGroup` — so the row is unmounted from one subtree and mounted into
 * another, and every piece of local state in it is gone. That lands on exactly
 * the wrong keystroke: a Fail or a Can't check OPENS this box on purpose,
 * because an answer of either kind without a sentence is one nobody can act on
 * — and if that answer was the last one outstanding, the box the surface just
 * asked you to fill in vanished as it appeared, along with anything already
 * typed into it.
 *
 * Pinning the row to its old group instead would break the invariant the whole
 * module rests on (the badge is the first group's length, by construction), so
 * the draft is what moves. A plain module map rather than a store: it is
 * scratch text belonging to a dialog that is open, and `forgetNoteDrafts`
 * empties it when that dialog closes so nothing leaks into the next visit. */
const NOTE_DRAFTS = new Map<string, { open: boolean; text: string }>();

function noteKey(planId: string, stepId: string): string {
  return planId + "\u0000" + stepId;
}

/** Per-row UI that has to survive the row being RE-PARENTED.
 *
 * The same hazard `NOTE_DRAFTS` exists for, and it bites hardest at the worst
 * moment. A row lives inside its group's subtree, so a plan that changes group
 * — which is exactly what a run FINISHING does — is unmounted and mounted
 * again, and every `useState` in it goes back to its initial value. The inline
 * run terminal therefore vanished at precisely the moment the failure it was
 * showing arrived, and could not be brought back: opening it is only reachable
 * while the plan is still `running`. A half-typed rewrite note and a pending
 * confirm went the same way.
 *
 * Keyed by plan id, module-level, cleared when the dialog closes — scratch UI
 * belonging to a dialog that is open, never a fact about the flock. */
interface RowUi {
  watching?: boolean;
  rewriting?: boolean;
  focus?: string;
  confirming?: "" | "delete" | "early";
}

const ROW_UI = new Map<string, RowUi>();

function rowUi(planId: string): RowUi {
  return ROW_UI.get(planId) || {};
}

function setRowUi(planId: string, patch: RowUi): void {
  ROW_UI.set(planId, { ...rowUi(planId), ...patch });
}

function forgetNoteDrafts(): void {
  NOTE_DRAFTS.clear();
  ROW_UI.clear();
}

function StepRow({
  step,
  index,
  plan,
  onRecord,
  onRunStep,
  running,
  onRemove,
  onEdit,
}: {
  step: TestStep;
  index: number;
  plan: TestPlan;
  /** Resolves when the server has taken the answer; rejects with its message. */
  onRecord(step: TestStep, result: TestStepResult, note: string): Promise<void>;
  /** Start a run scoped to this one step. Absent when the plan cannot be run
   * at all (still generating, or already has a session working it). */
  onRunStep?: (step: TestStep) => Promise<void>;
  running?: boolean;
  /** Delete this step. Present only for a step a person added — see
   * `test_plans.remove_step` for why a generated one has no such button. */
  onRemove?: (step: TestStep) => Promise<void>;
  /** Change this step's wording or who answers it. Absent while a run is in
   * flight — the agent is working from these sentences right now. */
  onEdit?: (step: TestStep, fields: Partial<TestStep>) => Promise<void>;
}) {
  const [pending, setPending] = useState<TestStepResult | null>(null);
  // Seeded from the draft map and written back through, so the composer is
  // where you left it even if the row was re-parented under another heading
  // between two keystrokes — see NOTE_DRAFTS.
  const draftKey = noteKey(plan.id, step.id);
  const draft = NOTE_DRAFTS.get(draftKey);
  const [editing, setEditing] = useState(false);
  const [noteOpen, setNoteOpenRaw] = useState(!!draft?.open);
  const [note, setNoteRaw] = useState(draft?.text || "");
  const [saving, setSaving] = useState(false);

  // `text` is explicit rather than read from the render-time closure: the
  // clear-then-open path passed the OLD sentence back into the draft map, so a
  // note you discarded came back on the next paint and was posted, by blur, as
  // the reason for the next answer. See `noteDraftAfter`.
  const setNoteOpen = (next: boolean, text: string = note) => {
    setNoteOpenRaw(next);
    if (next) NOTE_DRAFTS.set(draftKey, { open: true, text });
    else NOTE_DRAFTS.delete(draftKey);
  };
  const setNote = (next: string) => {
    setNoteRaw(next);
    NOTE_DRAFTS.set(draftKey, { open: true, text: next });
  };

  const entry = stepResult(plan, step.id);
  const check = stepCheck(plan, step.id);
  const shown = pending ?? entry?.result ?? "";
  // Who the answer on screen belongs to. While a POST is in flight the answer
  // being shown is the one YOU just gave, whatever the stored entry says — so a
  // Can't check settles the row instantly rather than un-highlighting a beat
  // later when the server's copy arrives.
  const shownBy = pending !== null ? "human" : entry?.by || "";
  // One rule, shared with the plan-level rules in verify.ts, so a step can
  // never look answered to the row and unanswered to the badge (or the other
  // way round). Pass, Fail, or a Can't check that YOU recorded — see
  // `isYourAnswer` for why the agent's blocked is not one.
  const answered = isYourAnswer(shown, shownBy);
  // WHOSE STEP IT IS NOW, not whose the model wrote it to be: an agent that came
  // back `blocked` has said the step needs a person, and from that moment this
  // row is a human row — the lane label, the accent and the counts on the card
  // above all read the same rule (`stepIsYours`).
  const handed = handedBack(entry);
  const mine = stepIsYours(plan, step);
  // The call to action: a human step nobody has settled, on a plan that has
  // SHIPPED. Deliberately not gated on a run existing any more — that made
  // human steps on a due plan look exactly like agent ones until you answered
  // some unrelated step, at which point every remaining human step lit up at
  // once, a visual change caused by a click that had nothing to do with them.
  // `planShipped`, not a third copy of the state list: a rewrite of a live
  // checklist sits in `generating`, and the copy here used to drop the accent
  // from every human row for the three minutes that takes.
  const wantsYou = mine && !answered && asksHumanSteps(plan);

  // Whether a note may be typed here at all.
  //
  // POST /result hard-codes `by="human"` and re-stamps `at`, so re-posting an
  // answer the AGENT settled — in order to carry a note — would silently
  // rewrite its authorship to you and its timestamp to now, on the one surface
  // whose whole job is to record who actually checked what. So a pass or a fail
  // the agent recorded offers Undo but not a note.
  //
  // An UNSETTLED step is a different thing. "blocked · agent" is the agent
  // explicitly declining to answer — every rule in verify.ts already treats it
  // as unchecked — and it is precisely the step you came here to answer, so the
  // box opens and what you type rides along with the answer you then give
  // (`record` posts it). Nothing is re-posted, so nothing is rewritten.
  const mayNote = !answered || !entry || entry.by === "human";
  // ...and Save only exists when there IS a human answer to re-post against.
  // Without one, the note has nothing to attach to, and inventing a result to
  // hang it on is the one thing this surface must never do.
  //
  // A Can't check YOU recorded is such an answer, and this is the case that was
  // silently broken: the box opens itself after one (it is exactly the answer
  // that needs a sentence), and with the old pass/fail-only test there was no
  // Save, blur saved nothing and Enter saved nothing — so the reason you typed
  // was thrown away by the surface that had just asked you for it.
  const mayResave = answered && (!entry || entry.by === "human");

  const record = async (result: TestStepResult) => {
    setPending(result);
    try {
      // An un-answer posts an empty note as well as an empty result. The
      // composer's text described an answer that no longer exists, and posting
      // it here used to store {result:"", note:"<the sentence>"} — cleared on
      // screen, persisted in the run, and re-rendered under the unanswered
      // step (plus re-seeded into the composer) on the next paint. Undo takes
      // this path too, so it sheds the same stale sentence.
      await onRecord(step, result, result === "" ? "" : note.trim());
      // ONE RULE, out in verify.ts where a test can reach it: un-answering
      // sheds the sentence, a fail or blocked with no sentence opens an EMPTY
      // box asking for one, and a note that rode along with the answer stops
      // being a draft (it renders above the row now).
      const draftNext = noteDraftAfter(result, noteOpen, note);
      setNoteRaw(draftNext.text);
      setNoteOpen(draftNext.open, draftNext.text);
    } catch (err) {
      errorPop("Couldn't record that answer", errMsg(err));
    } finally {
      setPending(null);
    }
  };

  /** Re-post the SAME answer, carrying the note. Splitting the two is what stops
   * a typed sentence being silently discarded because you never pressed a
   * button you had already pressed.
   *
   * Guarded by `mayResave`, not merely by "there is an answer": Save is not the
   * only way in — blur calls this too — and re-posting an answer the AGENT gave
   * would restamp it as yours. On an unsettled step this is a no-op on purpose,
   * and the note is carried by `record` instead when you pick an answer. */
  const saveNote = async () => {
    if (!mayResave) return;
    setSaving(true);
    try {
      await onRecord(step, shown, note.trim());
      setNoteOpen(false);
    } catch (err) {
      errorPop("Couldn't save that note", errMsg(err));
    } finally {
      setSaving(false);
    }
  };

  const stepMenu: MenuItem[] = [];
  // Only ever offered for an agent step: a human step has no machine answer to
  // ask for, which is what `actor` means, and the route refuses one by id as
  // well. Useful after a fix — the alternative is re-running a twelve-step plan
  // to re-read step 3.
  if (!mine && onRunStep)
    stepMenu.push({
      key: "recheck",
      label: running ? "Re-checking…" : "Re-check this step",
      title: "Start a session that checks only this step",
      // A no-op while its own re-check is already starting: the item stays for
      // the label, not for a second press.
      onSelect: () => {
        if (!running) void onRunStep(step);
      },
    });
  if (onEdit) {
    // FIXING ONE SENTENCE MUST NOT COST THE CHECKLIST. Before this, a generated
    // step that was slightly wrong — right check, wrong endpoint; handed to a
    // person when a shell could settle it — could only be corrected by rewriting
    // the whole plan: a model call, three minutes, and every answer against
    // every step that changed. That is a wildly disproportionate price for a
    // typo, and it is why people stop correcting a checklist and start
    // distrusting it instead.
    stepMenu.push({
      key: "edit",
      label: "Edit this step",
      title:
        "Change the wording. It becomes yours, so the next rewrite keeps it — " +
        "and any answer already recorded against it is cleared, because the " +
        "question changed.",
      onSelect: () => setEditing(true),
    });
    stepMenu.push({
      key: "actor",
      // The flip is worth its own item rather than living inside the editor: it
      // is the only way out of a checklist an agent cannot run at all (an
      // unrecognised actor is coerced to "human", and the run route refuses a
      // plan where every step is a person's), and it costs no answers.
      label: mine ? "Let the agent check this" : "I'll check this one",
      title: mine
        ? "An agent will settle it from a shell on the next run. Answers already " +
          "recorded are kept — only who answers changes."
        : "Take it off the agent — a run will leave it for you.",
      onSelect: () =>
        void onEdit(step, { actor: mine ? "agent" : "human" }),
    });
  }
  if (step.manual && onRemove)
    stepMenu.push({
      key: "remove",
      label: "Remove this step",
      title: "You added this one, so nothing will bring it back",
      danger: true,
      onSelect: () => void onRemove(step),
    });

  return (
    <div
      className={
        "vf-step" +
        (wantsYou ? " vf-wants-you" : "") +
        (shown === "fail" ? " vf-step-bad" : "")
      }
      data-step-id={step.id}
      data-manual={step.manual ? "1" : undefined}
      // ANSWERING IS A KEYBOARD JOB. It is the most repeated act on this
      // surface — a twelve-step checklist is twelve of them — and it used to
      // cost a Tab past roughly seven focusable controls per step, because the
      // "Answer N steps" button only ever scrolled the row into view and left
      // focus on itself.
      //
      // The guard is not optional: this row contains a textarea and two inputs,
      // and without it typing "pass" into a note would record a Pass, a Fail
      // and two Can't-checks on the way past. Anything originating inside a
      // field is left entirely alone.
      onKeyDown={(e) => {
        const from = e.target as HTMLElement;
        if (from !== e.currentTarget && from.closest("input, textarea, select"))
          return;
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        // The same guard the three answer buttons have had all along, plus the
        // one only a keyboard needs: a held key repeats ~15 times a second, and
        // every repeat was its own POST.
        if (!stepKeyAllowed(pending !== null || saving, e.repeat)) return;
        if (stepKeyIsUndo(e.key)) {
          if (!answered) return;
          e.preventDefault();
          void record("");
          return;
        }
        const action = stepKeyAction(e.key);
        if (!action) return;
        e.preventDefault();
        if (action === "note") {
          if (mayNote) setNoteOpen(true);
          return;
        }
        void record(action);
      }}
      tabIndex={-1}
    >
      {/* THE CHECK MARK. One glyph per row, at a fixed x, saying where this
          step stands before you read a word of it — the thing a checks list is
          for and the thing a list of prose paragraphs cannot do. It is not a
          fourth answer button: it is the same state the roll-up above counted,
          rendered per row so the column and the summary can never disagree. */}
      <span
        className="vf-step-mark"
        data-check={check}
        aria-hidden="true"
        title={CHECK_LABEL[check]}
      >
        {CHECK_MARK[check]}
      </span>
      <span className="vf-step-n">{index + 1}</span>
      {/* WHOSE STEP IS THIS. Both lanes are labelled, at one fixed x, before the
          text — the whole reason it is here rather than in a chip after the
          title. A chip on the human steps alone made "the agent does this" a
          thing you had to infer from an ABSENCE, at a right edge that moves with
          the length of every step; you cannot scan that. Two labels in a column
          you can run your eye down, you can. */}
      <span
        className={"vf-step-who" + (mine ? " vf-who-you" : "")}
        title={
          handed
            ? "The agent's step, handed back: it went and tried, and said it " +
              "couldn't settle this one — the reason is on the row. It is yours " +
              "now. A later run has another go at it."
            : mine
              ? "Only a person can answer this — visual judgement, a real browser, " +
                "or a service the agent has no tool for. A verify run leaves it " +
                "blocked with a reason."
              : "The agent's — checkable from a shell or its own tools (log " +
                "searches and dashboards included), so a verify run settles it " +
                "without you. You can still override its answer."
        }
      >
        {mine ? "you" : "agent"}
      </span>
      <div className="vf-step-body">
        {editing && onEdit ? (
          <StepEditor
            step={step}
            onCancel={() => setEditing(false)}
            onSave={async (fields) => {
              await onEdit(step, fields);
              setEditing(false);
            }}
          />
        ) : (
          <>
            <div className="vf-step-head">
              <span className="vf-step-text">{step.text}</span>
            </div>
            {step.expect ? (
              <div className="vf-step-expect">Expect: {step.expect}</div>
            ) : null}
          </>
        )}
        {/* The agent's own words about the step — for a blocked one this is the
            "what a person must do" line the run prompt asked it for, which is
            the most useful sentence on the row. */}
        {entry?.note ? <div className="vf-step-note">{entry.note}</div> : null}
        <div className="vf-step-actions">
          {/* Three answers, not two. "Blocked" is not a hedge — it is the
              honest one when you couldn't get to the thing (staging is down,
              you have no account on it), and without it a step you cannot check
              gets marked pass to make the row go away. */}
          {(["pass", "fail", "blocked"] as const).map((choice) => {
            // Shown as the recorded value, but only "pressed" when it is YOURS:
            // an agent's blocked is it declining to answer, and painting that as
            // a chosen answer would tell the reader the step was settled by the
            // very button that is asking them to settle it.
            const on = shown === choice && (answered || choice !== "blocked");
            return (
              <button
                key={choice}
                type="button"
                className={"vf-mark vf-mark-" + choice}
                data-on={on ? "1" : undefined}
                // The pressed state, for anyone not looking at the fill. Three
                // buttons where one is "on" is a radio group in everything but
                // markup, and without this a screen reader reads three
                // identical unpressed buttons.
                aria-pressed={on}
                aria-label={answerLabel(choice) + " — step " + (index + 1)}
                // `saving` too, not just `pending`: Save (and blur) re-post the
                // SAME answer with a note, and a click landing between that
                // request and its response is a second write racing the first —
                // whose last-arriving response need not be the last one the
                // server processed. The keyboard path applies the identical
                // rule through `stepKeyAllowed`.
                disabled={pending !== null || saving}
                title={
                  on
                    ? shownBy === "human"
                      ? "Recorded — press again to take it back"
                      : "The agent recorded this — Undo takes it back"
                    : "Record " + answerLabel(choice).toLowerCase()
                }
                // A second press on YOUR OWN lit answer takes it back. This
                // was the original behaviour, removed over the worry that a
                // reflex double-click would silently un-answer a step — and
                // then asked for outright by the person the worry was about
                // ("if pass or fail is lit up, if I click again it should go
                // off"). The owner's gesture wins. Undo stays beside the
                // receipt for anyone who reads before pressing, and both
                // paths run through record("") so the note-clearing rules
                // cannot diverge.
                //
                // YOUR OWN is load-bearing, and it takes the `shownBy` check
                // to mean it: `answered` alone (isYourAnswer) consults
                // authorship only for blocked, so the agent's lit Pass would
                // read as toggleable and one stray click would erase its
                // answer AND its note — evidence no second click can restore.
                // The agent's lit pass/fail therefore stays inert, exactly as
                // before: clearing it is Undo's job, and re-recording it here
                // would restamp its authorship as yours (the thing `saveNote`'s
                // guard exists to prevent).
                //
                // And `on && answered`, NOT `on`, decides which press this is.
                // A human step the agent left blocked already shows Can't
                // check as the recorded value, and reading that press as a
                // toggle would CLEAR the agent's "not mine" instead of
                // recording yours — the button must record on precisely the
                // steps this surface exists to collect.
                onClick={() => {
                  if (on && answered) {
                    if (shownBy === "human") void record("");
                    return;
                  }
                  void record(choice);
                }}
              >
                {pending === choice ? "…" : answerLabel(choice)}
              </button>
            );
          })}
          <span className="vf-step-tail">
            {/* Who said so, and when. This is the fact the whole surface exists
                to preserve, and it used to live in a title attribute. */}
            {entry && !pending && entry.result ? (
              <>
                {/* `data-result` stays the WIRE value — it drives the colour in
                    the stylesheet, and the store's vocabulary belongs in the
                    attribute. The text is what a person reads, so it comes from
                    the same map the button does. */}
                <span className="vf-receipt" data-result={entry.result}>
                  {receiptLabel(entry)} · {byLabel(entry.by)}
                  {ageOf(entry.at) ? " · " + ageOf(entry.at) : ""}
                </span>
                <button
                  type="button"
                  className="vf-undo"
                  disabled={pending !== null || saving}
                  title="Take this answer back — the step goes back to unchecked"
                  onClick={() => void record("")}
                >
                  Undo
                </button>
              </>
            ) : null}
            {mayNote && !noteOpen ? (
              <button
                type="button"
                className="vf-notetoggle"
                onClick={() => {
                  setNote(entry?.by === "human" ? entry.note || "" : "");
                  setNoteOpen(true);
                }}
              >
                + note
              </button>
            ) : null}
          </span>
          {stepMenu.length ? (
            <OverflowMenu label={"More for step " + (index + 1)} items={stepMenu} />
          ) : null}
        </div>
        {noteOpen ? (
          <div className="vf-step-note-row">
            <input
              type="text"
              className="vf-step-input"
              placeholder={shown === "pass" ? "anything worth recording?" : "what happened?"}
              aria-label={"Note for step " + (index + 1)}
              autoComplete="off"
              value={note}
              disabled={saving}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void saveNote();
                }
                if (e.key === "Escape") {
                  // Local, and stopped: Escape in a text box means "never mind
                  // this box", not "close the whole dialog".
                  e.stopPropagation();
                  setNoteOpen(false);
                }
              }}
              // Blur saves too, because the commonest way to lose a sentence is
              // to type it and then click the next step.
              onBlur={() => {
                if (mayResave && note.trim() && note.trim() !== (entry?.note || ""))
                  void saveNote();
              }}
            />
            {mayResave ? (
              <button
                type="button"
                className="test-btn vf-note-save"
                disabled={saving}
                onClick={() => void saveNote()}
              >
                {saving ? "Saving…" : "Save"}
              </button>
            ) : (
              // Nothing of yours to attach it to yet — the step is unanswered,
              // or the answer on it is the agent's and re-posting would restamp
              // it as yours. So the note waits and travels WITH the answer you
              // pick, which `record` sends. Saying that beats a Save button that
              // would have to invent a result to hang the sentence on.
              //
              // Phrased as the instruction it is. "goes with the answer you
              // pick" describes the mechanism and leaves the reader to work out
              // that they are supposed to do something.
              <span className="vf-note-hint">pick an answer — this saves with it</span>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** One plan: a three-line header that says what it is, whose turn it is and what
 * to press, and an expanded body carrying the steps.
 *
 * The header is three lines and one button on purpose. It used to be a title,
 * four chips and three equally-weighted buttons, where the rarest and only
 * irreversible action (Delete) had the same visual weight as the one thing you
 * opened the dialog to press. */
function PlanRow({
  plan,
  liveBranch,
  selection,
  expanded,
  onToggle,
  onExpand,
}: {
  plan: TestPlan;
  /** The flock-wide default, used ONLY as a stand-in when this plan's own repo
   * has not been resolved (an older payload). What the row compares against is
   * `plan.effective_live_branch` — see `nowLive` below. */
  liveBranch: string;
  /** The dialog's checkbox selection, when the list is selectable. */
  selection?: RowSelection;
  expanded: boolean;
  onToggle(): void;
  /** Open the body if it is closed — what "Answer 2" needs, which is not a
   * toggle: pressing it on an already-open plan must not shut it. */
  onExpand(): void;
}) {
  const closeDialog = useUi((s) => s.closeDialog);
  const openVerifyPane = useUi((s) => s.openVerifyPane);
  const [busy, setBusy] = useState("");
  const [stepBusy, setStepBusy] = useState("");
  const [focusStep, setFocusStep] = useState("");
  // All three are seeded from ROW_UI and written back through it, so a row that
  // changes group mid-gesture — which is what a run finishing does — comes back
  // exactly as it was. See ROW_UI.
  const savedUi = rowUi(plan.id);
  /** The inline rewrite composer, open or not. Replaces a native `confirm()`. */
  const [rewriting, setRewritingRaw] = useState(!!savedUi.rewriting);
  const setRewriting = (next: boolean) => {
    setRewritingRaw(next);
    setRowUi(plan.id, { rewriting: next });
  };
  /** The one destructive action left, asked inline for the same reason: a
   * browser `confirm()` inside an `aria-modal` dialog is a second window the app
   * did not draw and cannot phrase properly. "" = nothing pending. */
  const [confirming, setConfirmingRaw] = useState<"" | "delete" | "early">(
    savedUi.confirming || "",
  );
  const setConfirming = (next: "" | "delete" | "early") => {
    setConfirmingRaw(next);
    setRowUi(plan.id, { confirming: next });
  };
  /** Watch the run HERE rather than in a grid pane behind the modal.
   *
   * THREE STATES, not two, and that is what makes Hide work. Left alone the
   * watcher follows the run — expand a plan an agent is working and its output
   * is there, which is the behaviour this was built for. Pressing Hide or
   * "Show the run here" records a preference that outranks it, and pressing Run
   * latches it ON, so the terminal survives the moment the run ENDS: that is
   * when the failure it was streaming actually arrives, and it used to vanish
   * exactly then (the plan leaves `running`, and the row is re-parented into
   * another group, which remounts it — see ROW_UI). */
  const [watchPref, setWatchPrefRaw] = useState<boolean | null>(
    savedUi.watching === undefined ? null : savedUi.watching,
  );
  const setWatchingHere = (next: boolean) => {
    setWatchPrefRaw(next);
    setRowUi(plan.id, { watching: next });
  };
  const bodyRef = useRef<HTMLDivElement>(null);

  const run = latestRun(plan);
  const running = plan.state === "running";
  // THE SESSION THE LOG LIVES IN, which outlives `plan.run_session`: every
  // terminal transition blanks that field (`finish_run`, `cancel_run`,
  // `fail_run`, `mark_due`), so keying the watcher on it alone meant the
  // terminal — and the ⋯ item offering to bring it back — disappeared at the
  // instant the run ended. The run RECORD keeps the title, and the sweep
  // deliberately keeps that session open for exactly this reason: so the person
  // can read what it did.
  const watchSession = plan.run_session || run?.session || "";
  const watchingHere = watchPref === null ? running : watchPref;
  const evidence = runEvidence(run, plan.live_branch || liveBranch);
  const failedCount = failCount(plan);
  const steps = plan.steps || [];
  const status = planStatus(plan, liveBranch);
  const open = openHumanSteps(plan);
  // The split, counted once. Said in the legend so "which of these are mine?"
  // is answered before you start reading rows, and so a plan that is entirely
  // one lane says so instead of leaving you to check every row for a label.
  // Counted through `stepIsYours`, like every other count on this card: a run
  // that handed six steps back moved them into your lane, and a legend still
  // reading "6 for the agent" would be describing the checklist as written
  // rather than the one on screen — directly contradicting the eight ● marks
  // underneath it.
  const mineCount = steps.filter((s) => stepIsYours(plan, s)).length;
  const agentCount = steps.length - mineCount;
  // What THIS PLAN'S REPO calls live today, which is the only branch the plan's
  // own stamp can honestly be compared with. "What counts as shipped" is a
  // per-repo fact and a plan records the per-repo answer, so measuring it
  // against the flock-wide default would flag every plan in a repo that set its
  // own live branch as written against a branch that has moved — while the
  // branch it names is precisely the one the repo is configured for. The
  // flock-wide value survives only as the fallback for a payload old enough not
  // to carry the per-repo answer.
  const nowLive = plan.effective_live_branch || liveBranch;

  // WHERE THE WORK IS RIGHT NOW, in one word, and it is the only word on this
  // line that is not the branch. `plan.branch` is where it was pushed and
  // `live_branch` is what this checklist is waiting for; between the two, a
  // change in a repo that ships through staging spends most of its life merged
  // somewhere neither field names — so the row names the rung it is actually
  // standing on and lets the reader watch it climb: local → staging → main.
  //
  // It REPLACED "shipped 4d ago". "Shipped" was a verb this surface had to
  // teach (shipped where? this repo ships from `release`), it was the same word
  // for every plan past the first rung, and it said nothing a reader triaging a
  // list of eight actually wants — which of these is still only on my laptop,
  // which got as far as staging, which is out. The moment stays; only the noun
  // in front of it changes, so the line still dates itself.
  //
  // `local` is not a branch name and is deliberately not styled like one: it is
  // the absence of a landing, which is exactly the state a checklist spends its
  // first hours in. `live_at` without a `merged_into` is an older payload — the
  // rung that answers "merged into what?" is newer than the one that stamps
  // "this is live" — so it falls back to the branch the plan is measured
  // against rather than reporting a shipped plan as local.
  const where = plan.merged_into || (plan.live_at ? nowLive || "shipped" : "local");
  // "" for a landing the rung that answered could not date — a squash merge is
  // known by its PR, and a PR does not carry the moment it merged.
  const whereAgo = agoOf(plan.merged_into_at || plan.live_at || plan.generated_at);
  const whereTitle = (
    plan.merged_into
      ? [
          "Most recently merged into " +
            plan.merged_into +
            " on origin" +
            (whereAgo ? " " + whereAgo : "") +
            ".",
          // The trail, when there is one: staging on the day it merged, main on
          // the day somebody promoted it. One name per landing, so this is short.
          (plan.merged_into_all || []).length > 1
            ? "Also in " + (plan.merged_into_all || []).slice(1).join(", ") + "."
            : "",
          plan.merged_into === nowLive
            ? ""
            : "This repo ships from " + (nowLive || "?") + ".",
        ]
      : [
          plan.live_at
            ? "Live on " + (nowLive || "the live branch") + "."
            : "Still only on " + (plan.branch || "its own branch") + " — origin has it nowhere else yet.",
          plan.generated_at && !plan.live_at
            ? "Checklist written " + agoOf(plan.generated_at) + "."
            : "",
          plan.live_at ? "" : "This repo ships from " + (nowLive || "?") + ".",
        ]
  )
    .filter(Boolean)
    .join("\n");

  // "Answer N" opens the plan and puts the first thing it is asking about in
  // the middle of the screen. Expanding a twelve-step plan and leaving the
  // reader at step 1 to hunt for the two that are theirs is most of the work
  // the button was supposed to save.
  useEffect(() => {
    if (!focusStep || !expanded) return;
    const root = bodyRef.current;
    if (root) {
      const esc = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(focusStep) : focusStep;
      const el = root.querySelector('[data-step-id="' + esc + '"]');
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        // ...and FOCUS it. Scrolling a row into view and leaving focus on the
        // button you pressed is most of the work the button was supposed to
        // save: the row is where the keys work, and where a screen reader has
        // to be for any of this to have happened at all.
        (el as HTMLElement).focus?.({ preventScroll: true });
      }
    }
    setFocusStep("");
  }, [focusStep, expanded]);

  /** Start a run, optionally scoped to one step (the per-step Re-check).
   *
   * One function for both because they are the same request — a whole-plan run
   * is just this with no filter — so the optimistic patch, the sidebar refresh
   * and the error handling cannot drift between the two. */
  const startRun = async (only?: string[]) => {
    if (only) setStepBusy(only[0]);
    else setBusy("run");
    try {
      const r = await api<{ session?: string; plan?: TestPlan }>(
        planPath(plan.id) + "/run",
        { method: "POST", json: only ? { steps: only } : {} },
      );
      // The server already has a provisioning row for the verify session: pull
      // it now rather than leaving the grid unable to attach a pane to a
      // session it has never heard of, which is what Watch needs a moment later.
      await refreshInstances();
      // THE SERVER'S OWN ROW when it sends one. Hand-synthesising `{state,
      // run_session}` left out the run record `start_run` had just opened, so
      // for the ten seconds until the poll the row said an agent was checking
      // with no "last checked" line and no give-up clock behind it.
      const started = r?.plan;
      patchPlan(plan.id, (p) =>
        started || { ...p, state: "running", run_session: r?.session || "" },
      );
      // Latched, not left to follow `running`: the terminal has to still be
      // there when the run ENDS, which is the moment whatever it found arrives.
      setWatchingHere(true);
      await refreshTestPlans();
      // The row itself changes — the sentence becomes "An agent is checking the
      // steps it can" and the button becomes Watch — which is the feedback that
      // was missing. It deliberately does NOT teleport you into the session any
      // more: triaging three due plans lost the list on the first one, and a
      // pane opening behind a modal is invisible, so the button read as broken.
      // Watch is that trip, kept as one deliberate press.
      // "Starting", not "is checking": at this instant the route has answered
      // 202 and nothing has been provisioned yet — the worktree, the branch and
      // tmux all happen afterwards, and when they fail the app's only statement
      // about this run must not have been a claim that it was under way.
      // `session.create_failed` retracts it (see bridgeTestPlanEvents).
      toast("Starting an agent on " + planName(plan) + " in " + (r?.session || "a new session"));
    } catch (err) {
      errorPop("Couldn't start the run for " + planName(plan), errMsg(err));
      // The optimistic state above never landed on a failure, but the plan may
      // have moved for another reason (it 409s when a run is already going), so
      // take the server's word for where it actually is.
      void refreshTestPlans();
    } finally {
      setBusy("");
      setStepBusy("");
    }
  };

  const watch = () => {
    // Never a silent no-op. The session can go away between the row rendering
    // and the press, and returning quietly made the button look broken — no
    // pane, no message, nothing. Say it, and pull the plan so the row stops
    // offering a window onto something that is not there.
    if (!watchSession) {
      errorPop(
        "Nothing to watch",
        "That run's session is gone. The row will catch up in a moment — start it again from there.",
      );
      void refreshTestPlans();
      return;
    }
    openVerifyPane(watchSession);
    // The dialog HAS to close: the verify pane is a grid window and this is a
    // full-screen `.modal` over it, so a pane opened behind the dialog is a
    // press with no visible effect. The pane's own head carries the way back
    // (SpecialPane's "Verify" button), which is what makes the round trip
    // symmetrical rather than one-way.
    closeDialog();
  };

  /** The first step matching a result the latest run recorded.
   *
   * What the expanding buttons scroll to. Read off the same run `planStatus`
   * counted, so a button cannot land on a step that is no longer the one its
   * sentence is about. `by` narrows it to who recorded the answer, which is the
   * only way to tell your "Can't check" from the agent's "blocked". */
  const firstStepWith = (result: TestStepResult, by = ""): string => {
    const results = run?.results || {};
    return (
      steps.find((s) => {
        const entry = results[s.id];
        return entry?.result === result && (!by || entry.by === by);
      })?.id || ""
    );
  };

  /** Ask for a new checklist, optionally saying what the last one got wrong.
   *
   * NO `confirm()`. This used to open a native OS modal from inside an
   * `aria-modal` dialog — a second window, painted by the browser rather than by
   * this app, whose only choices were OK and Cancel — and it was skipped
   * entirely when nothing had been answered yet, so the one press that could
   * lose work was the one that asked, and the common press asked nothing.
   *
   * Worse, it threw away the highest-signal input in the building. The person
   * pressing Rewrite has just READ the weak checklist and knows exactly what
   * should have been checked instead; the old route took no body at all, so a
   * second press re-ran the identical prompt and hoped for a different answer.
   * The composer replaces both: the warning is a line of text next to the box
   * (and only when there is something to lose — see `rewriteWarning`), and the
   * box is where the correction goes. */
  const regenerate = async (focus = "") => {
    setBusy("regen");
    try {
      await api(planPath(plan.id) + "/regenerate", {
        method: "POST",
        json: { focus: focus.trim() },
      });
      // 202 means accepted, not finished — the headless one-shot runs in a
      // background thread for up to three minutes. "generating" is therefore
      // true from this moment, and showing it immediately is what stops a
      // second press while nothing visibly changes.
      //
      // Deliberately NOT followed by an invalidation: a re-read on this tick
      // would race the server's own write and could hand back the pre-request
      // state, flicking the row back to what it was. The truth arrives on its
      // own — `session.test_plan_ready` bridges straight to a refresh
      // (state/queries.ts), and the 10s poll is the backstop.
      // `gen_started` moves with the state, and it has to: the row calls a
      // `generating` plan stalled once its stamp is old enough, so patching the
      // state without the clock would put this plan straight back into "writing
      // stopped part-way" — the row it was just rescued from.
      patchPlan(plan.id, (p) => ({
        ...p,
        state: "generating",
        error: "",
        gen_started: Date.now() / 1000,
      }));
      setRewriting(false);
      toast("Rewriting the checklist for " + planName(plan) + " — up to three minutes");
    } catch (err) {
      errorPop("Couldn't rewrite " + planName(plan), errMsg(err));
      void refreshTestPlans();
    } finally {
      setBusy("");
    }
  };

  /** Run a plan whose commit has not shipped yet.
   *
   * A ONE-WAY DOOR, and the confirm is the only thing that says so. The run
   * checks out the plan's own commit rather than the live branch, which is
   * reasonable — but `finish_run` writes `state = "done"` whatever happened, and
   * the liveness loop only ever re-asks plans still in `generated`. So a plan
   * run early never comes back as due when the branch actually ships, which is
   * the opposite of what somebody pressing "run it now" is trying to achieve. */
  const runEarly = () => setConfirming("early");

  /** Stop the run without recording a verdict.
   *
   * The counterpart to Run, and needed for the same reason Run is worth
   * watching: it is minutes of a real agent on a real branch, so "not that
   * commit" / "it is stuck" has to have an answer that isn't waiting out the
   * server's two-hour give-up clock. The session is closed, not deleted — see
   * the endpoint — so whatever it found is still there to read. */
  const cancelRun = async () => {
    setBusy("cancel");
    try {
      const r = await api<{ session?: string; plan?: TestPlan }>(
        planPath(plan.id) + "/cancel",
        { method: "POST" }
      );
      if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
      // The session this pane was watching is ending, so take the window with
      // it rather than leaving a "Verifying" head over a dead socket. App.tsx
      // reaps orphans as a backstop; closing it here is what makes the press
      // feel like it did something.
      if (plan.run_session) useUi.getState().closeVerifyPane(plan.run_session);
      // The verify session has just left the grid; the sidebar must not keep
      // showing it as live work.
      await refreshInstances();
      await refreshTestPlans();
      toast(r?.session ? "Stopped " + r.session : "Run cancelled");
    } catch (err) {
      errorPop("Couldn't cancel the run", errMsg(err));
      void refreshTestPlans();
    } finally {
      setBusy("");
    }
  };

  /** "It's out — check it now", skipping the rest of the deploy wait.
   *
   * The delay is a good guess and never a fact: a pipeline that usually takes
   * fifteen minutes sometimes takes three, and somebody who just watched it land
   * should not have to wait out a timer that has already been proved wrong. */
  /** Open a session to fix what the check found.
   *
   * The press this surface was missing. A red checklist is the most valuable
   * thing the feature produces — it shipped, it does not do what it was for,
   * and somebody wrote down exactly how — and the only thing you could do with
   * one was read it. An ORDINARY session, never the verify one: a verify run's
   * whole posture is "report, never fix", and reusing it to make a change would
   * dismantle the property that makes its report evidence.
   *
   * Deliberately not the primary button on a failed row — the primary stays
   * "See what failed", because reading the evidence before spending a session on
   * it is the right order, and a step can simply be wrong. */
  const fixFailures = async (only?: string[]) => {
    setBusy("fix");
    try {
      const r = await api<{ session?: string; reclaimed?: boolean }>(
        planPath(plan.id) + "/fix",
        { method: "POST", json: only ? { steps: only } : {} },
      );
      await refreshInstances();
      // The cleanup is said out loud when there was one. A workspace being
      // reclaimed under you is the kind of thing that looks like a bug later if
      // nothing ever mentioned it — and it is also the answer to "why did this
      // press work when the last one didn't?".
      toast(
        "Opened " +
          (r?.session || "a session") +
          " to fix what failed" +
          (r?.reclaimed ? " (cleared the last one's empty workspace)" : ""),
      );
    } catch (err) {
      errorPop("Couldn't open a session to fix it", errMsg(err));
    } finally {
      setBusy("");
    }
  };

  const markDeployed = async () => {
    setBusy("deployed");
    try {
      const r = await api<{ plan?: TestPlan }>(planPath(plan.id) + "/deployed", {
        method: "POST",
      });
      if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
      await refreshTestPlans();
      toast(planName(plan) + " is ready to check");
    } catch (err) {
      errorPop("Couldn't release " + planName(plan) + " for checking", errMsg(err));
      void refreshTestPlans();
    } finally {
      setBusy("");
    }
  };

  const remove = async () => {
    setBusy("delete");
    try {
      await api(planPath(plan.id), { method: "DELETE" });
      // Deleting the plan closes its run session too (see the route), so the
      // watch window goes with it.
      if (watchSession) useUi.getState().closeVerifyPane(watchSession);
      // ...and so does its scratch UI. Titles are reused (a new session with the
      // same name produces the same plan id), so a "watching" flag left behind
      // would open a terminal on the NEXT checklist of that name for a session
      // that no longer exists.
      ROW_UI.delete(plan.id);
      queryClient.setQueryData<TestPlansResponse>(["test-plans"], (prev) =>
        prev ? { ...prev, plans: prev.plans.filter((p) => p.id !== plan.id) } : prev
      );
      await refreshTestPlans();
    } catch (err) {
      errorPop("Couldn't delete " + planName(plan), errMsg(err));
      void refreshTestPlans();
    } finally {
      setBusy("");
    }
  };

  const recordStep = async (step: TestStep, result: TestStepResult, note: string) => {
    // "Was this plan asking me for something, and has it stopped?" — asked of
    // the badge's own predicate rather than of the open-step count, so it also
    // covers the plan nobody ever ran: `openHumanSteps` requires a run, so a
    // shipped checklist answered entirely by hand went from asking to answered
    // with the count at 0 on both sides, and the one plan whose completion was
    // most entirely the user's doing was the one that got no acknowledgement.
    const wasAsking = isWaitingOnYou(plan);
    const answer = await api<{ plan?: TestPlan }>(planPath(plan.id) + "/result", {
      json: { step_id: step.id, result, note },
    });
    const next = answer?.plan;
    if (next) patchPlan(plan.id, () => next);
    // THE COMPLETION MOMENT, and the surface had none. You worked down a
    // checklist, gave the last answer, and nothing said so: the row simply
    // restyled itself and — a moment later — left for another group, which
    // reads as the thing you were working on vanishing rather than as finishing
    // it. The toast is the receipt, and it names where the row went so the
    // disappearance is an outcome instead of a glitch.
    if (next && wasAsking && !isWaitingOnYou(next)) {
      // Where it lands, asked of the SAME rule that will file it there, rather
      // than assumed to be "Checked" — a plan whose last answer was a Fail moves
      // to "Steps failed", and a toast that named the wrong group would be one
      // more thing on this surface that does not match what is on screen.
      const dest = groupPlans([next])[0]?.label || "";
      toast(
        "All your steps are answered — " +
          planName(plan) +
          (dest ? " moves to " + dest : ""),
      );
    }
    void refreshTestPlans();
  };

  const doPrimary = () => {
    if (status.action === "answer") {
      onExpand();
      // Three buttons share this action and they are asking three different
      // questions, so they land in three different places: "Answer N steps" on
      // the first step that is still yours, "See what failed" on the first
      // failure, "Check again" on the first thing you couldn't get to. Ordered
      // rather than switched, and every arm falls through to the next, so the
      // button always lands somewhere useful even if a result changed under it.
      const target =
        (status.tone === "bad" ? firstStepWith("fail") : "") ||
        (open.length ? open[0].id : "") ||
        firstStepWith("blocked", "human");
      if (target) setFocusStep(target);
      return;
    }
    if (status.action === "watch") {
      // The run is watched INSIDE the checklist now. The grid pane is still
      // there for anyone who wants the agent on screen while they work
      // elsewhere, but it is no longer the price of pressing Watch.
      onExpand();
      setWatchingHere(true);
      return;
    }
    if (status.action === "rewrite") {
      onExpand();
      setRewriting(true);
      return;
    }
    void startRun();
  };

  // Every applicable action EXCEPT the one already on the primary button.
  const menu: MenuItem[] = [];
  // BRING THE LOG BACK. Opening it inline is otherwise reachable only while the
  // plan is `running` (the primary button's "watch" arm), so the terminal that
  // vanished when the run finished — the moment its own failure arrived — could
  // not be reopened at all, and the only other route to it, below, closes the
  // dialog to show a grid pane. Offered whenever there is a session to show.
  if (watchSession && !watchingHere)
    menu.push({
      key: "watch-here",
      label: running ? "Show the run here" : "Show the last run here",
      title:
        watchSession +
        " — its output, inline under this checklist, without closing the dialog",
      onSelect: () => setWatchingHere(true),
    });
  if (watchSession)
    menu.push({
      key: "watch",
      label: "Open the session in its own window",
      title:
        watchSession +
        " — the same stream this checklist shows inline, in a grid pane you can " +
        "keep open while you work somewhere else. Closing it does not stop the run.",
      onSelect: watch,
    });
  if (running)
    menu.push({
      key: "cancel",
      label: "Cancel run",
      title:
        "Stops the session and puts this plan back. Nothing it found is lost — " +
        "the session is closed, not deleted, and Recent… reopens it.",
      className: "test-btn vf-cancel",
      onSelect: () => void cancelRun(),
    });
  // Only while it is actually waiting on a pipeline — see the route, which
  // refuses anything else rather than silently reopening a finished checklist.
  if (plan.state === "generated" && plan.merged_at)
    menu.push({
      key: "deployed",
      label: "It's deployed — check it now",
      title: "Skip the rest of this repo's deploy window and make it due now",
      onSelect: () => void markDeployed(),
    });
  if (!running && steps.length && plan.state !== "generating") {
    // Whether an agent could do anything here at all. A checklist whose every
    // step is a person's own is not runnable — the run prompt forbids the agent
    // from settling one — so offering it would spend a worktree and minutes of
    // a billed session to hand everything straight back.
    const agentCan = steps.some((s) => s.actor !== "human");
    if (status.action === "none" && agentCan)
      // The pre-live plan. Its only run is the early one, and it is named for
      // what it costs rather than dressed as an ordinary Run. Gated on
      // `agentCan` like every other run offer: without that, a checklist whose
      // every step is a person's own showed this as its ONLY action, and the
      // run route refuses it — a row whose single button always errors.
      menu.push({
        key: "early",
        label: "Check it early…",
        title:
          "Runs an agent against this checklist's own commit rather than the live " +
          "branch — and closes it, so it will not come back when the branch ships",
        onSelect: runEarly,
      });
    else if (agentCan && status.action !== "run" && status.action !== "rerun")
      // Every state whose primary button is something else — notably a recorded
      // failure, whose button now opens the evidence. Re-running after a fix is
      // the right thing to do second, not first.
      menu.push({
        key: "run",
        label: run ? "Run again with an agent" : "Run with an agent",
        title: "Starts a session that works every step it can from a shell",
        onSelect: () => void startRun(),
      });
  }
  // Only when there is something to fix — a button that opens an empty session
  // is worse than no button, and the route 409s it anyway.
  if (!running && failedCount > 0)
    menu.push({
      key: "fix",
      label:
        failedCount === 1
          ? "Fix what failed…"
          : "Fix the " + failedCount + " that failed…",
      title:
        "Opens an ordinary session in this repo with the step, what was expected " +
        "and what happened instead. It is told to reproduce it first — a step can " +
        "be wrong, and changing shipped code to satisfy a wrong step is worse.",
      onSelect: () => void fixFailures(),
    });
  if (plan.state !== "generating" && status.action !== "rewrite")
    menu.push({
      key: "regen",
      label: "Rewrite the checklist",
      title:
        "Ask the model again — and tell it what this draft got wrong, which is " +
        "the part that makes the second one better than the first",
      onSelect: () => {
        onExpand();
        setRewriting(true);
      },
    });
  menu.push({
    key: "delete",
    label: "Delete this checklist",
    danger: true,
    title: "Forget this checklist and everything recorded against it",
    onSelect: () => {
      onExpand();
      setConfirming("delete");
    },
  });

  return (
    <div
      className={"vf-plan" + (selection?.has(plan.id) ? " picked" : "")}
      data-tone={status.tone}
      data-plan-id={plan.id}
    >
      <div className="vf-plan-head">
        {selection ? (
          // BEFORE the expander, so the column of boxes is the first thing the
          // eye can run down — the same place Recently closed puts it. The
          // checkbox stops its own click, so ticking a row never expands it.
          <span className="vf-plan-check">
            <RowCheck
              checked={selection.has(plan.id)}
              title={"Select " + planName(plan) + " (Shift-click to extend the range)"}
              onToggle={(shift) => selection.toggle(plan.id, shift)}
            />
          </span>
        ) : null}
        {/* The whole title block is the expander. `.tk-caret` inside an
            aria-expanded button is Intake's affordance and the rotation rule in
            IntakeDialog.css reaches it here for free, because these rows render
            inside a WorkListPanel's `.ik-groups`. */}
        <button
          type="button"
          className="vf-plan-toggle"
          aria-expanded={expanded}
          onClick={onToggle}
          title={
            [
              planName(plan),
              "branch " + (plan.branch || "?"),
              "commit " + (plan.sha ? plan.sha.slice(0, 12) : "?"),
              "live branch " + (plan.live_branch || "?"),
              plan.merged_into ? "merged into " + plan.merged_into : "",
              plan.repo_root,
            ]
              .filter(Boolean)
              .join("\n")
          }
        >
          <span className="tk-caret">▸</span>
          <span className="vf-plan-title">{planName(plan)}</span>
          {/* WHAT SHIPPED, in a sentence. `planName` is the session's title —
              the key everything addresses this plan by — so a checklist coming
              due three weeks later was headed "sc-1234-fix-filters" over a list
              of imperatives, and the reader had to reconstruct what the change
              was from the steps themselves. The model writes this alongside the
              steps; a plan generated before it did simply has no second line. */}
          {plan.summary ? (
            <span className="vf-plan-summary">{plan.summary}</span>
          ) : null}
        </button>
        {/* THE status. One sentence, in the second person where it matters,
            replacing four chips that could disagree with each other and with
            the heading above them. Plain text, so a screen reader and a
            pointer-less user get exactly what the eye gets. */}
        {/* THE SENTENCE AND THE TALLY, on one line. The tally used to sit on
            the meta line below, where it was the third thing on a row of
            identifiers and read as more metadata; beside the sentence it reads
            as what it is — the same statement, counted. It also puts the two
            things a triaging eye runs down, "whose turn is it" and "is anything
            red", on the same baseline, so a list of eight is one pass. */}
        <div className="vf-plan-line">
          <div className="vf-stopline" data-tone={status.tone}>
            {/* THE WHEEL. `generating` is the one state with nothing to press
                and nothing on screen changing for up to three minutes; without
                motion, "working" and "wedged" are the same quiet row. The
                stalled branch flips the tone to "broken" and takes the wheel
                with it — a spinner beside "stopped part-way" would be the row
                calling itself a liar. */}
            {plan.state === "generating" && status.tone === "wait" ? (
              <span className="vf-spin" aria-hidden="true" />
            ) : null}
            {status.line}
          </div>
          <PlanTally plan={plan} />
        </div>
        {/* The branch, and the rung it is standing on. No short sha: it was the
            middle of three identifiers on a line only wide enough for one and a
            half, it is not what anyone addresses this plan by, and the branch
            name it was stealing width from is. The full twelve are still in the
            row's own tooltip, where a sha is actually of use. */}
        <div className="vf-plan-meta">
          <span className="vf-plan-meta-text">{plan.branch}</span>
          {/* A chip rather than more meta text, because this is the one fact on
              the row somebody SCANS a list for — "which of these are in main
              already?" — and running text at 11px is not scannable. Tinted when
              the branch it names is the one this repo ships from, so "it's out"
              and "it's got as far as staging" are told apart without reading. */}
          <span
            className="vf-plan-landed"
            data-live={where === nowLive && !!nowLive ? "yes" : "no"}
            title={whereTitle}
          >
            {where}
            {whereAgo ? " · " + whereAgo : ""}
          </span>
        </div>
        {status.action !== "none" ? (
          <button
            type="button"
            className="btn-primary vf-primary"
            data-action={status.action}
            // `stepBusy` too: a per-step Re-check and this button start the SAME
            // request, and two of them in flight for one plan is how a title
            // gets claimed twice — one create wins, the loser's failure lands on
            // the winner's plan, and the winner's tmux session is left with
            // nothing owning it. The server refuses the second now; the button
            // should not offer it.
            disabled={
              busy !== "" ||
              stepBusy !== "" ||
              (status.action === "watch" && !plan.run_session)
            }
            title={
              status.action === "answer"
                ? status.tone === "bad"
                  ? "Open the steps that didn't do what was expected"
                  : status.tone === "warn"
                    ? "Open the steps you couldn't get to"
                    : "Open the steps that need your eyes"
                : status.action === "watch"
                  ? "Open " + (plan.run_session || "the verify session")
                  : status.action === "rewrite"
                    ? "Ask the model for a new set of steps from the same diff"
                    : // The cost, said plainly. This is the one control on the
                      // surface that spends a workspace and minutes of a billed
                      // agent, and it used to be indistinguishable from "run the
                      // tests".
                      "Starts a real session in its own workspace and works every " +
                      "step it can from a shell — minutes, not seconds. Steps only " +
                      "you can judge come back for you."
            }
            onClick={doPrimary}
          >
            {busy === "run" ? "Starting…" : busy === "regen" ? "…" : status.actionLabel}
          </button>
        ) : null}
        <OverflowMenu label={"More actions for " + planName(plan)} items={menu} />
      </div>

      {expanded && (
        <div className="vf-plan-body" ref={bodyRef}>
          {!!plan.error && (
            // The generator failing is a defect in the plumbing, not an answer
            // about the code, so it gets the error verbatim rather than a
            // friendly paraphrase — the raw line is what makes it fixable.
            //
            // Keyed on the ERROR and not on `state === "failed"`, because a
            // rewrite that fails no longer costs the plan its rung: a checklist
            // with steps keeps its place in the queue and records the reason
            // here (see `test_plans._fail`). Rendering this only for `failed`
            // meant the commonest failure — a rewrite of a good checklist timing
            // out — reported itself nowhere at all.
            <div className="vf-error">
              {/* Neutral, because more than one thing writes here now: a
                  generation that failed, a rewrite that failed, and a verify
                  session that could not start. The stored sentence says which —
                  each writer stores a self-describing one — so a heading that
                  guessed would be wrong a third of the time. */}
              <strong>{steps.length ? "That didn't work." : "Couldn't write a plan."}</strong>{" "}
              {plan.error || "no reason recorded"}
              <div className="vf-note">
                {steps.length
                  ? "The steps below are untouched, and still answerable."
                  : "Rewriting asks the model again. If it keeps failing, the coding CLI " +
                    "it used probably has no headless mode configured, or the worktree it " +
                    "was written from is gone — you can also write the steps yourself below."}
              </div>
            </div>
          )}
          {/* The second entrance to the one-way door, and the quieter one:
              `record_result` closes ANY plan to "done" the moment nothing is
              unanswered, liveness be damned. So a helpful person working through
              a pre-live checklist by hand silently removes it from the due
              pipeline — with nothing anywhere on the screen having said so. */}
          {plan.state === "generated" && steps.length > 0 && (
            <div className="vf-warn">
              Answering these now closes the checklist — it will not come back to
              <strong> Not checked yet</strong> when the branch ships.
            </div>
          )}
          {/* The plan was measured against whatever ITS REPO counted as live
              when its answers were recorded. A checklist NOBODY has answered
              follows the repo's current setting on its own — even back out of
              `due` (`retarget_live_branch`) — so by the time this can persist
              the difference is a fact about answers already given rather than
              something still to fix. (An unanswered plan can show it for up to
              a minute, until the due loop's next pass re-aims it — which is
              what the last clause promises.) The comparison is against the
              repo's own branch and not the flock-wide default, which would
              fire on every plan in a correctly-configured repo and teach the
              reader to ignore it. */}
          {plan.live_branch && nowLive && plan.live_branch !== nowLive && (
            <div className="vf-drift">
              Measured against <code>{plan.live_branch}</code>; this repo now ships{" "}
              <code>{nowLive}</code>. Answers here were recorded against that branch, so it
              keeps it — a checklist with nothing answered re-aims itself at{" "}
              <code>{nowLive}</code> on its own, within a minute.
            </div>
          )}
          {run && (
            <div className="vf-note">
              Last checked by {byLabel(run.by)}
              {run.session ? " (" + run.session + ")" : ""}, {agoOf(run.at) || "just now"}.
              {/* WHAT THAT RUN IS EVIDENCE ABOUT. The only place this surface
                  can honestly answer "did it validate production", and the
                  answer genuinely differs per repo — a deployment, or a checkout
                  of the live branch on this machine. */}
              {evidence ? " " + evidence + "." : ""}
            </div>
          )}
          {runTreeMismatch(run) && (
            // The failure `build_run_prompt` calls the one that must not be able
            // to happen, made visible when it does. The server has already
            // downgraded that run's passes; this says why, so the row is not a
            // mystery full of blocked steps.
            <div className="vf-warn">
              That run wasn't on {nowLive || "the live branch"} — it worked{" "}
              <code>{(run?.tested_sha || "").slice(0, 7)}</code>, so its passes have been
              set aside. Run it again.
            </div>
          )}
          {steps.length > 0 && (
            // The one sentence a new user genuinely must read, said once per
            // plan where it is unmissable. It used to be three `title`
            // attributes, which is a strange place to keep the promise the
            // whole surface rests on.
            <div className="vf-legend">
              {/* Two sentences, in the order a reader needs them: first whose
                  step is whose, then what the three answers mean. The lane
                  labels are quoted in their own colours so the line teaches the
                  column beside it rather than describing it. */}
              <div>
                <span className="vf-step-who vf-who-you">you</span> = only a person can
                check it ·{" "}
                <span className="vf-step-who">agent</span> = a verify run settles it from
                a shell
                {mineCount > 0 && agentCount > 0
                  ? " — " + agentCount + " for the agent, " + mineCount + " for you"
                  : ""}
                .
              </div>
              {/* The one sentence a new user must read, and the promise the
                  whole surface rests on: all three are answers, and only one of
                  them says it works. Spelling that out is what makes "Can't
                  check" safe to press — before it, the honest answer was the one
                  that left the row nagging, so people pressed Pass instead. */}
              <div>
                <strong>Pass</strong> = it did what's expected · <strong>Fail</strong> = it
                didn't · <strong>Can't check</strong> = you couldn't get to it. All three
                count as your answer; only Pass says it works.
              </div>
              {/* Not optional furniture: none of this is discoverable, and the
                  legend is the one place per plan where this surface's
                  vocabulary is taught rather than assumed. */}
              <div className="vf-keys">
                On a step: <kbd>1</kbd> pass · <kbd>2</kbd> fail · <kbd>3</kbd> can't
                check · <kbd>u</kbd> undo · <kbd>n</kbd> note.
              </div>
            </div>
          )}
          {rewriting && (
            <RewriteBox
              plan={plan}
              busy={busy === "regen"}
              onCancel={() => setRewriting(false)}
              onRewrite={(focus) => void regenerate(focus)}
            />
          )}
          {confirming === "delete" && (
            <ConfirmBox
              title={"Delete the checklist for " + planName(plan) + "?"}
              body={
                "Every answer recorded against it goes too. " +
                (running
                  ? "This also stops " + (plan.run_session || "the verify session") + "."
                  : "A later push of the same branch writes a new one.")
              }
              confirmLabel={busy === "delete" ? "Deleting…" : "Delete it"}
              busy={busy !== ""}
              onCancel={() => setConfirming("")}
              onConfirm={() => {
                setConfirming("");
                void remove();
              }}
            />
          )}
          {confirming === "early" && (
            <ConfirmBox
              title={"Check it before it ships?"}
              body={
                planName(plan) +
                " hasn't reached " +
                (plan.live_branch || nowLive || "the live branch") +
                " yet, so this checks out its own commit (" +
                (plan.sha ? plan.sha.slice(0, 7) : "its branch") +
                ") rather than what users have. Once it finishes, this checklist is " +
                "closed — it will NOT come back to \u201cNot checked yet\u201d when the " +
                "branch does ship."
              }
              confirmLabel="Check it early"
              busy={busy !== ""}
              onCancel={() => setConfirming("")}
              onConfirm={() => {
                setConfirming("");
                void startRun();
              }}
            />
          )}
          {/* THE RUN, HERE. Watching used to mean closing this whole dialog so a
              grid pane could open behind it — which threw away the checklist you
              were reading, on the surface where the checklist is the point, and
              left every step the agent handed back sitting unanswered in a window
              you had just been thrown out of. The same read-only stream renders
              inline, so "watch the agent" and "answer the steps it left you" are
              one screen. The pane is still one ⋯ item away for anyone who wants
              the agent on screen while they work elsewhere. */}
          {watchSession && watchingHere && (
            <RunWatch
              session={watchSession}
              onPopOut={watch}
              onHide={() => setWatchingHere(false)}
            />
          )}
          {steps.length > 0 && <ChecksHead plan={plan} />}
          {steps.length > 0 ? (
            <div className="vf-steps">
              {steps.map((step, i) => (
                <StepRow
                  key={step.id}
                  step={step}
                  index={i}
                  plan={plan}
                  onRecord={recordStep}
                  // Withheld while a session is already working this plan: a
                  // second run would race the first for the same result file.
                  // Withheld while anything else is starting a run — two
                  // starts for one plan race the same result file, and the
                  // server now refuses the second — but NOT while this step's
                  // own re-check is starting: the row wants to keep its item
                  // and relabel it "Re-checking…", which is the only feedback
                  // between the menu click and the toast seconds later.
                  onRunStep={
                    running || busy !== "" || (stepBusy !== "" && stepBusy !== step.id)
                      ? undefined
                      : (s) => startRun([s.id])
                  }
                  running={stepBusy === step.id}
                  // Withheld while a run is in flight for the same reason
                  // `onRunStep` is: the agent is working from these exact
                  // sentences right now, and the server 400s it anyway.
                  onEdit={
                    running
                      ? undefined
                      : async (st, fields) => {
                          try {
                            const r = await api<{ plan?: TestPlan }>(
                              planPath(plan.id) +
                                "/steps/" +
                                encodeURIComponent(st.id),
                              { method: "PATCH", json: fields }
                            );
                            if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
                            void refreshTestPlans();
                          } catch (err) {
                            errorPop("Couldn't change that step", errMsg(err));
                          }
                        }
                  }
                  onRemove={async (st) => {
                    try {
                      const r = await api<{ plan?: TestPlan }>(
                        planPath(plan.id) + "/steps/" + encodeURIComponent(st.id),
                        { method: "DELETE" }
                      );
                      if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
                      void refreshTestPlans();
                    } catch (err) {
                      errorPop("Couldn't remove that step", errMsg(err));
                    }
                  }}
                />
              ))}
            </div>
          ) : null}
          {/* Not while the plan is still being written: its step list is about
              to be replaced wholesale, and a step added into that window would
              look like it had been thrown away. Every other state can take one,
              including a plan that failed to generate — writing the checklist
              by hand is a perfectly good answer to "the model could not". */}
          {plan.state !== "generating" && (
            <details className="vf-addstep-fold">
              <summary>Add a step of your own</summary>
              <AddStep
                plan={plan}
                onAdded={(next) => {
                  patchPlan(plan.id, () => next);
                  void refreshTestPlans();
                }}
              />
            </details>
          )}
        </div>
      )}
    </div>
  );
}

/** "Add a step of your own" — the composer under a plan's steps.
 *
 * The generator reads a diff. It cannot know the flow that always breaks in
 * this product, the report nobody remembers to open, or the customer who will
 * phone about it — and a checklist that cannot take those is one people keep a
 * second copy of somewhere else, which is the copy that never gets run.
 *
 * Added steps survive a regeneration (they are marked `manual` server-side), so
 * rewriting the plan re-asks the model about the diff without deleting the half
 * of the list that came from somebody's head.
 *
 * Behind a disclosure, because a checklist you are reading should not have a
 * form in the middle of it. */
/** One step, being corrected in place.
 *
 * Two fields and two buttons, rendered where the step's own text was — not in a
 * dialog, because a dialog over a dialog is the pattern this whole surface has
 * just spent an afternoon getting rid of, and because the sentence you are
 * fixing should stay where you were reading it.
 *
 * Saving clears any answer recorded against this step, and the server decides
 * that rather than the row: it drops the answer only when the QUESTION changed
 * (text or expect), never when only the actor moved. Said in the ⋯ item's title
 * rather than shouted here — it is the correct behaviour and the alternative
 * (an answer to a question nobody asked) is worse. */
function StepEditor({
  step,
  onCancel,
  onSave,
}: {
  step: TestStep;
  onCancel(): void;
  onSave(fields: Partial<TestStep>): Promise<void>;
}) {
  const [text, setText] = useState(step.text);
  const [expect, setExpect] = useState(step.expect || "");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    ref.current?.focus();
  }, []);

  const save = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await onSave({ text: text.trim(), expect: expect.trim() });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="vf-step-edit">
      <textarea
        ref={ref}
        className="vf-step-edit-text"
        rows={2}
        aria-label="What to do"
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onCancel();
          }
        }}
      />
      <input
        type="text"
        className="vf-step-edit-expect"
        aria-label="What should happen"
        placeholder="what should happen"
        autoComplete="off"
        value={expect}
        disabled={busy}
        onChange={(e) => setExpect(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void save();
          }
          if (e.key === "Escape") {
            e.stopPropagation();
            onCancel();
          }
        }}
      />
      <div className="vf-step-edit-actions">
        <button
          type="button"
          className="test-btn"
          disabled={busy || !text.trim()}
          onClick={() => void save()}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" className="test-btn" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/** The roll-up line above the steps — the "checks" summary.
 *
 * WHY THIS EXISTS NEXT TO A STATUS SENTENCE THAT ALREADY SAYS SOMETHING.
 * `planStatus` is one ordered decision producing one sentence, which is what
 * stops the row contradicting itself — but it can only ever say the LOUDEST true
 * thing ("2 steps need your eyes"), and the question somebody has on opening a
 * twelve-step checklist is a different one: how much of this is settled, and
 * how. That is the question a checks panel answers at a glance, and it is why
 * every CI surface has one. Computed from the same run the sentence read, so the
 * two are two renderings of one state rather than two opinions.
 *
 * Zeroes are omitted rather than shown as "0 failed". A row of zeroes reads as a
 * form, and the one number that matters is the one that is not zero. */
function ChecksHead({ plan }: { plan: TestPlan }) {
  const t = checkTally(plan);
  return (
    <div className="vf-checks-head">
      <span className="vf-checks-total">
        {t.total} {t.total === 1 ? "check" : "checks"}
      </span>
      {tallyBits(plan).map((bit) => (
        <span key={bit.state} className="vf-checks-bit" data-check={bit.state}>
          <span className="vf-mark-glyph" aria-hidden="true">
            {CHECK_MARK[bit.state]}
          </span>
          {bit.count} {bit.label}
        </span>
      ))}
    </div>
  );
}

/** THE SAME TALLY, ON THE COLLAPSED ROW — where the decision is actually made.
 *
 * Everything a person needs to triage a list of checklists was one click inside
 * each one: the row said whose turn it was ("2 steps need your eyes") and the
 * roll-up said where the work had got to (5 passed, 1 failed, 3 not started),
 * and only the first of those was visible without opening the plan. On a list
 * of eight, that is eight expansions to answer "is anything red, and how far
 * along is any of this" — which is the question the surface exists to answer.
 *
 * Glyph + number, not words: it shares its line with the status sentence, which
 * is the one thing on the row allowed to be long, and five labelled bits would
 * push it into an ellipsis two words from its start. The
 * words are still there for anyone who needs them — the `title`, and the
 * announced sentence beside it — and the glyphs and colours are the ones the
 * step rows and the legend already use, so nothing here is a new vocabulary.
 * Colour is never the only signal: each bucket has its own mark (✓ ✗ ● – ○). */
function PlanTally({ plan }: { plan: TestPlan }) {
  const bits = tallyBits(plan);
  const sentence = tallySentence(plan);
  if (!bits.length) return null;
  return (
    // `role="img"` + a label is what makes a group of glyphs announce as one
    // sentence instead of as five bare numbers; there is no visually-hidden
    // utility class in this app and inventing a global one for a tally would be
    // the wrong shape of change.
    <span className="vf-plan-tally" role="img" aria-label={sentence} title={sentence}>
      {bits.map((bit) => (
        <span
          key={bit.state}
          className="vf-plan-tally-bit"
          data-check={bit.state}
          aria-hidden="true"
        >
          <span className="vf-mark-glyph">{CHECK_MARK[bit.state]}</span>
          {bit.count}
        </span>
      ))}
    </span>
  );
}

/** "Rewrite the checklist", with the one input that makes the second draft
 * better than the first.
 *
 * The old flow was a native `confirm()` — an OS-painted window opened from
 * inside an `aria-modal` dialog, offering OK and Cancel — followed by three
 * minutes of nothing. It asked only when a run existed, so the press that could
 * lose work was the one that stopped to ask and the ordinary press asked
 * nothing; and it took no note, so a second press re-ran the identical prompt
 * and hoped.
 *
 * The box is optional and the button works empty, because "just try again" is a
 * real request — a generation that timed out needs no correction. But it is
 * PRESENT, and the placeholder says what kind of sentence helps, because the
 * person here has just read a checklist that missed the point and is the only
 * one who knows what it missed. */
function RewriteBox({
  plan,
  busy,
  onCancel,
  onRewrite,
}: {
  plan: TestPlan;
  busy: boolean;
  onCancel(): void;
  onRewrite(focus: string): void;
}) {
  // Through ROW_UI for the same reason the row's own flags are: a run finishing
  // under you re-parents this box's owner and takes the sentence with it —
  // three minutes of thinking about what the last checklist missed, gone
  // because an unrelated agent reported.
  const [focus, setFocusRaw] = useState(rowUi(plan.id).focus ?? (plan.focus || ""));
  const setFocus = (next: string) => {
    setFocusRaw(next);
    setRowUi(plan.id, { focus: next });
  };
  const ref = useRef<HTMLTextAreaElement>(null);
  const blocked = rewriteBlockedReason(plan);
  const warning = rewriteWarning(plan);

  // Opening a box nobody is standing in is a box nobody types in.
  useEffect(() => {
    ref.current?.focus();
  }, []);

  return (
    <div className="vf-rewrite">
      <div className="vf-rewrite-title">Rewrite the checklist</div>
      <textarea
        ref={ref}
        className="vf-rewrite-text"
        rows={2}
        aria-label="What should the new checklist check instead?"
        placeholder={
          "What should it check instead? (optional) — e.g. \u201cfocus on the " +
          "coupon flow at checkout, ignore the settings refactor\u201d"
        }
        value={focus}
        disabled={busy || !!blocked}
        onChange={(e) => setFocus(e.target.value)}
        onKeyDown={(e) => {
          // Enter is a newline here, not a submit: this is prose, and a
          // sentence somebody is halfway through is not a request.
          // Escape closes the box rather than the dialog behind it — the same
          // rule AddStep follows, for the same reason.
          if (e.key === "Escape") {
            e.stopPropagation();
            onCancel();
          }
        }}
      />
      {blocked ? (
        <div className="vf-rewrite-warn">{blocked}</div>
      ) : warning ? (
        <div className="vf-rewrite-warn">{warning}</div>
      ) : null}
      <div className="vf-rewrite-actions">
        <button
          type="button"
          className="btn-primary"
          disabled={busy || !!blocked}
          onClick={() => onRewrite(focus)}
        >
          {busy ? "Asking…" : "Rewrite"}
        </button>
        <button type="button" className="test-btn" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        <span className="vf-rewrite-note">
          It keeps the steps you wrote or edited yourself, and takes up to three minutes.
        </span>
      </div>
    </div>
  );
}

/** An inline "are you sure", for the two actions that genuinely are one-way.
 *
 * Same job a `confirm()` did and none of its problems: it is drawn by this app
 * so it can use this app's words and more than two of them, it does not steal
 * focus from the window behind it, and it cannot be suppressed by a browser that
 * has decided this page shows too many dialogs. */
function ConfirmBox({
  title,
  body,
  confirmLabel,
  busy,
  onCancel,
  onConfirm,
}: {
  title: string;
  body: string;
  confirmLabel: string;
  busy: boolean;
  onCancel(): void;
  onConfirm(): void;
}) {
  return (
    <div className="vf-confirm">
      <div className="vf-confirm-title">{title}</div>
      <div className="vf-confirm-body">{body}</div>
      <div className="vf-confirm-actions">
        <button
          type="button"
          className="test-btn vf-confirm-go"
          disabled={busy}
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
        <button type="button" className="test-btn" disabled={busy} onClick={onCancel}>
          Keep it
        </button>
      </div>
    </div>
  );
}

/** The verify session, streamed into the checklist that asked for it.
 *
 * Read-only ON PURPOSE — `useWsTerm(..., false)` sets `disableStdin`. The run is
 * working a checklist it was given and its answers are the artifact; typing at it
 * mid-run produces a report about a conversation nobody can reconstruct later. If
 * you want to take over, the session is real and one ⋯ item away in its own
 * window. */
function RunWatch({
  session,
  onPopOut,
  onHide,
}: {
  session: string;
  onPopOut(): void;
  onHide(): void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  // RECONNECTING, unlike the grid pane. This watcher mounts the instant Run is
  // pressed — that is its whole point — and at that instant the session does not
  // exist: `create_instance` returns as soon as the record is registered and cuts
  // the worktree in the background. The first socket is refused, and without a
  // retry the box sat blank and "disconnected" forever while the run started
  // perfectly normally behind it.
  const state = useWsTerm(
    hostRef,
    "/api/instances/" + encodeURIComponent(session) + "/terminal",
    false,
    true
  );
  return (
    <div className="vf-runwatch">
      <div className="vf-runwatch-head">
        <span className="vf-runwatch-title">{session}</span>
        <span className="vf-runwatch-state" data-state={state}>
          {state === "starting"
            ? "starting the session…"
            : state === "reconnecting"
              ? "reconnecting…"
              : state === "streaming"
                ? "live"
                : state}
        </span>
        <button
          type="button"
          className="test-btn"
          onClick={onPopOut}
          title="Open it as a grid window so you can keep it while you work elsewhere"
        >
          Own window
        </button>
        <button
          type="button"
          className="test-btn"
          onClick={onHide}
          title="Hide this view — the run keeps going"
        >
          Hide
        </button>
      </div>
      <div className="vf-runwatch-term" ref={hostRef} />
    </div>
  );
}

function AddStep({ plan, onAdded }: { plan: TestPlan; onAdded(next: TestPlan): void }) {
  const [text, setText] = useState("");
  const [expect, setExpect] = useState("");
  const [actor, setActor] = useState<TestStepActor>("agent");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    const body = text.trim();
    if (!body) return;
    setBusy(true);
    try {
      const r = await api<{ plan?: TestPlan }>(planPath(plan.id) + "/steps", {
        method: "POST",
        json: { text: body, expect: expect.trim(), actor },
      });
      if (r?.plan) onAdded(r.plan);
      // Cleared only on success, so a failed POST does not cost you the
      // sentence you just typed.
      setText("");
      setExpect("");
    } catch (err) {
      errorPop("Couldn't add that step", errMsg(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="vf-addstep">
      <input
        type="text"
        className="vf-addstep-text"
        placeholder="what to do"
        aria-label="New step"
        autoComplete="off"
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          // Enter adds. There is no enclosing form here (the dialog is not one),
          // so this cannot leak into a submit the way the New Session folder
          // field could — but it is still stopped explicitly rather than left
          // to the absence of a form somebody may add later.
          if (e.key === "Enter") {
            e.preventDefault();
            void add();
          }
          // Escape means "never mind this box", not "throw away the dialog".
          // Without it, the one field on this surface holding something a person
          // WROTE — rather than picked — is the one field an Escape destroys.
          if (e.key === "Escape") e.stopPropagation();
        }}
      />
      <input
        type="text"
        className="vf-addstep-expect"
        placeholder="what should happen (optional)"
        aria-label="Expected result"
        autoComplete="off"
        value={expect}
        disabled={busy}
        onChange={(e) => setExpect(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void add();
          }
          if (e.key === "Escape") e.stopPropagation();
        }}
      />
      <select
        className="vf-addstep-actor"
        aria-label="Who checks this step"
        value={actor}
        disabled={busy}
        onChange={(e) => setActor(e.target.value as TestStepActor)}
      >
        <option value="agent">the agent</option>
        <option value="human">me</option>
      </select>
      <button
        type="button"
        className="test-btn"
        disabled={busy || !text.trim()}
        onClick={() => void add()}
      >
        {busy ? "Adding…" : "Add"}
      </button>
    </div>
  );
}

/** The grouped list.
 *
 * Its own component so the two toggle sets are one hook pair rather than one
 * per plan, and so the dialog shell above stays about the shell. */
function PlanList({
  plans,
  liveBranch,
  selection,
}: {
  plans: TestPlan[];
  liveBranch: string;
  /** Absent when nothing can be selected (there is no such caller today, but a
   * row that cannot be picked must not render a box that does nothing). */
  selection?: RowSelection;
}) {
  const groups = useToggleSet(GROUPS_KEY, true);
  // Plans default CLOSED and the set holds the ones you opened — one polarity,
  // unlike the XOR this replaced, where membership meant "toggled away from
  // whatever my group implies" and a plan you had deliberately collapsed
  // re-opened itself when it changed group.
  const opened = useToggleSet(PLANS_KEY);

  return (
    <>
      {groupPlans(plans).map((g) => (
        <WorkGroup
          key={g.key}
          heading
          name={g.label}
          count={g.plans.length}
          detail={g.detail}
          open={groups.isOpen(g.key)}
          onToggle={() => groups.toggle(g.key)}
        >
          {g.plans.map((plan) => (
            <PlanRow
              key={plan.id}
              plan={plan}
              liveBranch={liveBranch}
              selection={selection}
              expanded={opened.isOpen(plan.id)}
              onToggle={() => opened.toggle(plan.id)}
              onExpand={() => {
                if (!opened.isOpen(plan.id)) opened.toggle(plan.id);
              }}
            />
          ))}
        </WorkGroup>
      ))}
    </>
  );
}

/** "Write a plan for…" — the button, and the normal way a plan comes to exist.
 *
 * Automatic generation is opt-in per repo (the Repositories list beside it, or
 * the repo's own committed `.mindflock.toml`) and off everywhere else, because
 * writing a plan costs a real model call and a Verify list that fills itself
 * destroys the one number this surface exists to show. That leaves a
 * chicken-and-egg problem this bar is the answer to: nobody opts a repo in to
 * something they have never watched work, so there has to be a way to ask for
 * exactly one plan, by name, with nothing configured.
 *
 * Only sessions with a branch and no plan yet are offered (`planTargets` picks
 * them). It renders even when that list is empty — disabled, saying why —
 * because a control that disappears makes the dialog a different shape on
 * different visits, and the empty state then has to carry a second variant
 * purely to avoid pointing at furniture that is not on the screen. */
function NewPlanBar({
  candidates,
  closed,
  reason,
}: {
  candidates: string[];
  /** Which candidates are closed sessions — labelled, because "this will not
   * open a window" is worth knowing before pressing the button. */
  closed: Set<string>;
  reason: string;
}) {
  const [busy, setBusy] = useState("");
  const [picked, setPicked] = useState("");

  // The picker holds a stale title once that session gets a plan (it leaves
  // `candidates` but stays in state), so resolve against the live list rather
  // than trusting it.
  const target = candidates.includes(picked) ? picked : candidates[0] || "";

  async function write() {
    setBusy(target);
    try {
      const r = await api<{ plan: string; existing: boolean }>(
        "/api/instances/" + encodeURIComponent(target) + "/test-plan",
        { method: "POST" }
      );
      // 202 means a model call is now running for up to three minutes. Say so:
      // the plan appears immediately as "writing the plan", and a user who was
      // not told would read that quiet row as the button having failed.
      toast(
        r.existing
          ? target + " already has a checklist — it is in the list below"
          // "up to three minutes", matching the row this press creates and the
          // server's own budget. It used to promise "a minute" 200px above a row
          // saying three, which makes the faster of the two promises a broken one.
          : "Writing a checklist for " + target + " — up to three minutes"
      );
      refreshTestPlans();
    } catch (err) {
      errorPop("Couldn't write a checklist", errMsg(err));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="vf-newplan">
      <label htmlFor="vf-newplan-pick">Write a checklist for</label>
      <select
        id="vf-newplan-pick"
        value={target}
        disabled={!!busy || !candidates.length}
        onChange={(e) => setPicked(e.target.value)}
      >
        {/* An empty select when there is nothing to offer, rather than an
            <option> carrying a sentence. A disabled dropdown whose only entry is
            a paragraph reads as a broken list of one; the reason belongs in the
            hint below, which is where this control already explains itself. */}
        {candidates.map((t) => (
          <option key={t} value={t}>
            {t}
            {closed.has(t) ? " (closed)" : ""}
          </option>
        ))}
      </select>
      <button
        type="button"
        id="vf-newplan-go"
        className="test-btn"
        disabled={!!busy || !candidates.length}
        onClick={() => void write()}
      >
        {busy ? "Writing…" : "Write it"}
      </button>
      {/* Why you cannot, when you cannot — and it is three different reasons.
          One sentence covered all of them and was simply false for the commonest
          one: telling somebody with five open sessions to start a session. */}
      <span className="set-hint">
        {reason || "Reads the branch's diff and writes a checklist of what to check."}
      </span>
    </div>
  );
}

/** The repositories Verify tracks, and what each of them calls live.
 *
 * The other half of the bargain the bar above makes. Writing a plan costs a real
 * model call, so nothing happens in a repo nobody named; adding one here is how
 * you say this repo has earned it — and where you say what "live" even means for
 * it — without leaving the dialog and without learning a config file exists.
 *
 * Membership IS the opt-in. There is no "automatic" checkbox any more: a repo on
 * this list gets a plan on every push of a session branch, exactly as a repo in
 * `github.repos` is watched by virtue of being there. A checkbox next to a list
 * you had to add the repo to anyway was two decisions for one intent, and the
 * off state was indistinguishable from having removed it.
 *
 * This is the SAME component the Intake tabs use (intake/RepoSources.tsx), on a
 * third `surface`, and that is the point. A repo is named the way GitHub names
 * it — you type `owner/name` — because that is how every other repo list in this
 * app is spelled, because it is stable across machines and clones, and because
 * the alternative this replaced (discovering local checkouts from whichever
 * sessions happened to be open) meant the list changed under you when you closed
 * a session, and keyed a machine-local absolute path that meant nothing to
 * anybody else in the flock.
 *
 * The live branch is per repo rather than one flock-wide setting because "what
 * counts as shipped" is a per-repo fact: `main` in one repo, `staging` in the
 * next, `release` in the third. One global answer was wrong the moment you
 * worked in two repos, and a plan measured against the wrong branch is worse
 * than no plan — it goes due when nothing shipped, or never goes due at all. A
 * blank field inherits, and its placeholder shows what it inherits, so "empty"
 * reads as "inherits staging" rather than as "nothing set".
 *
 * A checkout with no GitHub origin cannot be named here at all — there is no
 * slug to type. Its opt-in is the repo's own committed `.mindflock.toml`
 * (`[workspace] verify_on_push = true`), which is path-based, travels with the
 * code, and is OR'd with this list; neither can switch the other off. The
 * footnote below says so, because a user whose repo cannot appear here needs to
 * be told where else to look rather than left clicking Add.
 */
function VerifySources({
  liveBranch,
  deployDelay,
}: {
  liveBranch: string;
  /** The flock-wide deploy wait, in minutes — the cards' placeholder. */
  deployDelay: string;
}) {
  const s = useSettings();
  const repo = (s.settings.repository || {}) as {
    verify_repos?: string[];
    verify_repo_settings?: RepoOverrides;
  };
  const repos = Array.isArray(repo.verify_repos) ? repo.verify_repos : [];
  const overrides = (repo.verify_repo_settings || {}) as RepoOverrides;

  return (
    <RepoSourceList
      surface="verify"
      // "Sources", the word the two Intake tabs' equivalent block is called in
      // the code and the word this list actually is: where checklists come
      // from. It renders through `#verify-body > .set-row > .set-label`, which
      // is a re-anchored copy of Intake's own rule, so the heading above these
      // cards is the same uppercase rule as CHECKLISTS below them.
      label="Sources"
      repos={repos}
      overrides={overrides}
      // ONE save for both halves, as the Intake tabs do it: the list and the
      // per-repo blocks are one edit as far as the user is concerned, and
      // two POSTs would let a rename land without its settings — which is a
      // repo that silently came back with its live branch reset.
      onSave={(list, next, msg) =>
        s.saveGroup("repository", { verify_repos: list, verify_repo_settings: next }, msg)
      }
      // Only `liveBranch` means anything on this surface; the rest are the
      // Intake fields a verify card does not render (see `surface` there).
      defaults={{
        agent: "",
        baseBranch: "",
        minAge: "",
        skipAuthors: "",
        liveBranch,
        deployDelay,
      }}
      listId="vf-repos-list"
      addId="vf-repo-add-btn"
      addLabel="+ Add repository"
      emptyText="No repositories yet — add one and every session branch pushed in it gets a checklist."
      // Trimmed to the length of Intake's, which is the length a hint under an
      // always-visible section can be before it outweighs the cards above it.
      // The committed-opt-in story it used to carry in full is in web-ui.md and
      // configuration.md now; what survives is the pointer, because a user whose
      // repo has no GitHub slug has to be told this list is not their door.
      hint={
        <>
          Each card is one GitHub repository, matched to your checkouts by their{" "}
          <code>origin</code>, with its own live branch. Being on this list is the opt-in:
          the first push of a session branch in these repos gets a checklist. A repo with
          no GitHub remote opts in with <code>[workspace] verify_on_push = true</code> in
          its own <code>.mindflock.toml</code> instead.
        </>
      }
    />
  );
}

/** The master switch — the one piece of configuration that stays above the work.
 *
 * "Is this thing on?" must be answerable without opening anything: it is the
 * first question asked of a paused feature and the answer to "why is nothing
 * appearing?", so it costs its own band at the top. Everything else that is
 * configuration is :func:`VerifySources` below it and :func:`VerifyByHand`
 * under the work, which is where Intake keeps the same two things. */
function VerifySwitch() {
  const s = useSettings();
  const repo = (s.settings.repository || {}) as {
    verify_repos?: string[];
    verify_enabled?: boolean;
  };
  const n = Array.isArray(repo.verify_repos) ? repo.verify_repos.length : 0;
  // Absent => on, matching the backend default: the real gate is membership, so
  // an older settings file with no key behaves exactly as it did.
  const on = repo.verify_enabled !== false;

  return (
    <AutomationSwitch
      label="Check what ships, automatically"
      title="Turn automatic checking on or off — your repositories, checklists and answers are kept either way"
      rowId="vf-toggle-row"
      inputId="vf-enabled"
      statusId="vf-status"
      checked={on}
      onChange={(next) =>
        s.saveGroup(
          "repository",
          { verify_enabled: next },
          next ? "Automatic checking on" : "Automatic checking paused"
        )
      }
      // The one thing a switch genuinely cannot say for itself: it is ON and it
      // is watching nothing, which is the state every new install starts in and
      // the reason the list below is empty. Never shown to narrate on/off.
      note={
        n
          ? undefined
          : "Add a repository below and every session branch pushed in it gets a checklist"
      }
    />
  );
}

/** The by-hand writer, in the fold Intake keeps its once-a-month settings in.
 *
 * `.pr-advanced` is REUSED rather than re-cut, because it is the same kind of
 * thing in the same place: a `<details>` after the work list holding what you
 * touch rarely (PullRequestsTab.tsx / IssuesTab.tsx put theirs at the bottom of
 * the tab, and the rules live in screens.css). Borrowing the class means the two
 * surfaces cannot drift apart by a padding value.
 *
 * The sources above are how a checklist normally comes to exist. This is the
 * escape hatch, and it earns its place for one reason: nobody opts a repository
 * in to something they have never watched work, so there has to be a way to ask
 * for exactly one checklist, by name, with nothing configured. That is a
 * once-per-lifetime need, which is precisely what a fold is for.
 */
function VerifyByHand({
  candidates,
  closed,
  reason,
}: {
  candidates: string[];
  /** Which candidates are closed sessions, so the picker can say so. */
  closed: Set<string>;
  /** Why `candidates` is empty, from `noTargetsReason`; "" when it isn't. */
  reason: string;
}) {
  return (
    <details className="pr-advanced">
      <summary>Write a checklist by hand</summary>
      <div className="pr-advanced-body">
        <p className="set-hint set-block-hint">
          Reads a session branch's diff and writes a checklist for it now, without
          tracking its repository. Useful once, to see what the checklists look like
          before opting a repository in. Sessions you have already{" "}
          <strong>closed</strong> are offered too — asking for a checklist is
          something you do when the work is finished and the window has been put
          away, so refusing there made the button useless at the one moment you
          want it.
        </p>
        <NewPlanBar candidates={candidates} closed={closed} reason={reason} />
      </div>
    </details>
  );
}

/** The first thing anybody sees, and for a while the only thing.
 *
 * Not "nothing here": a surface nobody has fed yet looks broken, and the honest
 * answer is that nothing has shipped. What follows is the chain — which is
 * stated NOWHERE else in the product, in the UI or in the docs — because every
 * question a new user has about Verify is really "what has to happen before a
 * row appears here?", and it is three things in a fixed order.
 *
 * It points UP at Sources rather than carrying its own button. It used to have
 * one, because the repository list was behind a collapsed fold and a direction
 * to a fold is a direction to something the reader cannot see; now the list is
 * an always-visible section a few lines above, exactly as it is on the Intake
 * tabs, so naming it is enough and a second Add control would be two doors onto
 * one room. */
function VerifyEmpty({ liveBranch }: { liveBranch: string }) {
  return (
    <div className="repo-empty vf-empty">
      <p>
        <strong>Nothing has shipped that needs checking yet.</strong>
      </p>
      <ol className="vf-howto">
        <li>
          Add a repository under <strong>Sources</strong> above — every session branch
          pushed in it gets a checklist.
        </li>
        <li>The checklist waits while the branch is reviewed and merged.</li>
        <li>
          When that work reaches <code>{liveBranch || "the live branch"}</code>, it turns up
          here for you to check.
        </li>
      </ol>
    </div>
  );
}

export function VerifyDialog() {
  const open = useUi((s) => s.openDialog === "verify");
  const closeDialog = useUi((s) => s.closeDialog);
  // Polls only while the dialog is up; the top bar's badge keeps its own copy
  // of the same query alive for the count.
  const plansQ = useTestPlans(open);
  // Read here rather than inside the bar: the same list decides what the picker
  // may offer. The query is the app-wide one the sidebar and grid already keep
  // warm, so this costs no extra polling.
  const instancesQ = useInstances();
  // CLOSED SESSIONS COUNT TOO: asking for a checklist is something people do
  // once the work is finished and the window has been put away, which is
  // precisely when the session is no longer in `instancesQ`.
  //
  // DECLARED UP HERE WITH THE OTHER QUERIES, and that is not tidiness. This
  // component returns `null` when the dialog is shut (below), so a hook added
  // after that early return only exists on the renders where it is open — the
  // hook count changes the instant you press Verify, React throws "rendered more
  // hooks than during the previous render", and the entire app unmounts to a
  // white screen. Every hook in this component has to be above that line.
  //
  // `enabled: open` is what keeps it honest instead: the hook always runs, the
  // request only happens while somebody is looking.
  const closedQ = useQuery({
    queryKey: ["recently-closed"],
    queryFn: () => api<ClosedTarget[]>("/api/recently-closed"),
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });
  // The tracked-repo list is ordinary settings now, so this dialog needs the
  // same settings model the Intake tabs get from IntakeDialog — loaded on open,
  // provided below, and read by `VerifySwitch` / `VerifySources` through
  // `useSettings()`. Nothing Verify-specific: `repository.verify_repos` saves
  // through the one POST /api/settings every other card list already uses.
  const model = useSettingsModel(open);

  /** The Ctrl+F filter, and the checkbox selection it drives.
   *
   * ABOVE THE `if (!open)` RETURN, like every other hook here — see the note on
   * `closedQ` for what adding one below it costs. */
  const [query, setQuery] = useState("");
  const allPlans = plansQ.data?.plans || [];
  const shown = useMemo(() => {
    const tokens = searchTokens(query);
    return allPlans.filter((p) => planMatches(p, tokens));
  }, [allPlans, query]);
  // Both key lists follow the DISPLAY order — shift-ranges have to match what
  // the eye sees, and a bulk confirmation reads like the list it came from.
  // Grouping reorders within a group but never across the list's own order, so
  // the flat order is the one to select in.
  const sel = useRowSelection(
    useMemo(() => allPlans.map((p) => p.id), [allPlans]),
    useMemo(() => shown.map((p) => p.id), [shown]),
  );
  const [bulk, setBulk] = useState("");
  const [confirmBulk, setConfirmBulk] = useState(false);

  // Half-typed step notes belong to the visit, not to the app: drop them when
  // the dialog shuts so nothing reappears in a row a week later. The filter and
  // the selection go with them, for a sharper reason: a checkbox still ticked
  // from last time is how somebody deletes a checklist they never looked at.
  useEffect(() => {
    if (open) return;
    forgetNoteDrafts();
    setQuery("");
    setConfirmBulk(false);
    sel.clear();
  }, [open, sel]);

  // Every dialog re-implements this: there is no shared <Modal>, and the one
  // thing they all agree on is that Escape closes and the keydown listener is
  // only installed while the dialog is actually open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeDialog();
        e.preventDefault();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  // Put the caret somewhere sensible on open. Without this, a keyboard user
  // arriving via Alt+V has focus still on whatever was behind the modal, so the
  // first Tab walks the page UNDER the dialog — and on a surface whose whole job
  // is pressing one of three small buttons per step, that is the difference
  // between usable and not.
  useEffect(() => {
    if (!open) return;
    const id = window.setTimeout(() => {
      const panel = document.getElementById("verify-panel");
      // One selector at a time, in PRIORITY order. A single comma-separated
      // selector would not do: querySelector returns the first match in DOCUMENT
      // order, and #verify-close lives up in the header — so every visit would
      // have landed on Close, which is the one control nobody opened this dialog
      // to press.
      for (const sel of [".vf-primary", "#vf-repo-add-btn", "#verify-close"]) {
        const el = panel?.querySelector<HTMLElement>(sel);
        if (el) {
          el.focus();
          break;
        }
      }
    }, 0);
    return () => window.clearTimeout(id);
  }, [open]);

  if (!open) return null;

  const plans = allPlans;
  const byId = new Map(plans.map((p) => [p.id, p]));
  const picked = sel.keys.map((id) => byId.get(id)).filter(Boolean) as TestPlan[];
  const liveBranch = plansQ.data?.live_branch || "";
  // Does anything on this surface actually wait on a different branch? "What
  // counts as shipped" is per repo, so the one branch in the header is a
  // DEFAULT, not a fact about every plan — and a header that states it as a
  // fact contradicts the repo card in Sources, and the drift line on the rows.
  // Asked of the PLANS and of the CONFIGURED REPOS both: a plan outlives its
  // repo's place on the list, and a repo configured a minute ago has no plans
  // at all (they cost a model call and are written on push), so either source
  // alone leaves a case where the header lies. `liveBranchOverridden` owns the
  // rule and says why.
  const repoCfg = (model.settings.repository || {}) as {
    verify_repos?: string[];
    verify_repo_settings?: Record<string, { live_branch?: string }>;
    deploy_delay_minutes?: number;
  };
  // The flock-wide wait, shown as each card's placeholder so an empty field
  // reads as "inherits 5" rather than as "nothing set". Absent => the backend
  // default, which is 5.
  const deployDelay = String(
    repoCfg.deploy_delay_minutes == null ? 5 : repoCfg.deploy_delay_minutes
  );
  const overridden = liveBranchOverridden(
    liveBranch,
    plans,
    repoCfg.verify_repos,
    repoCfg.verify_repo_settings
  );
  const closed = closedQ.data || [];
  const targets = planTargets(instancesQ.data || [], plans, closed);
  const closedNames = closedTargets(instancesQ.data || [], closed);

  /** Fan a per-plan endpoint out over the selection, then reload once.
   *
   * ONE REQUEST PER PLAN, deliberately: each route carries its own refusals
   * (a run needs an agent step and a repo that still exists; a delete closes
   * that plan's session), and a bulk endpoint would have to re-implement every
   * one of them. What the bar owes the user instead is an honest tally — how
   * many went, and what the rest said. */
  const fanOut = async (
    targets: TestPlan[],
    verb: string,
    each: (plan: TestPlan) => Promise<unknown>,
  ) => {
    if (!targets.length) return;
    setBulk(verb);
    const failures: string[] = [];
    for (const plan of targets) {
      try {
        await each(plan);
      } catch (err) {
        failures.push(planName(plan) + " — " + errMsg(err));
      }
    }
    setBulk("");
    setConfirmBulk(false);
    sel.clear();
    await refreshInstances();
    await refreshTestPlans();
    const done = targets.length - failures.length;
    if (done) toast(verb + " " + done + " of " + targets.length);
    if (failures.length)
      errorPop(
        failures.length + (failures.length === 1 ? " checklist" : " checklists") +
          " couldn't be " + verb.toLowerCase(),
        failures.join("\n"),
      );
  };

  // Only the ones a run would actually take: the route refuses a checklist that
  // is generating, one an agent is already working, and one with no agent step
  // in it — so firing at all of them would answer a wall of 409s and start
  // nothing. Said in the button's own label rather than discovered afterwards.
  const runnable = picked.filter(canRunNow);

  const error = plansQ.error
    ? "Could not load the checklists: " + (plansQ.error.message || "error")
    : "";
  const note = panelNote({
    error,
    fetching: plansQ.isFetching,
    loaded: !!plansQ.data,
  });

  return (
    <SettingsCtx.Provider value={model}>
      <div
        id="verify-dialog"
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="verify-title"
        onClick={(e) => {
          if (e.target === e.currentTarget) closeDialog();
        }}
      >
        <div id="verify-panel">
          <div className="ws-head">
            <h2 id="verify-title">Verify</h2>
            <span className="ik-subtitle">What shipped, and does it work?</span>
            {/* The live branch, named out loud. "Live" is the entire premise of
                this surface — a plan is due because its commit reached THIS
                branch — and leaving the reader to guess which one it resolved to
                would make every row an assertion they cannot check. Shown only
                once the server has answered: guessing "main" here would be the
                one lie the surface cannot afford — and so is stating one branch
                for the whole flock when a repo in Sources has been given its own. */}
            {liveBranch ? (
              <span
                className="vf-live"
                title={
                  "A checklist sits quiet until its commit reaches this branch, then turns " +
                  "up here to be checked." +
                  (overridden
                    ? " Some repos ship from another branch — each checklist's own is on its row."
                    : "")
                }
              >
                checked once work reaches <code>{liveBranch}</code>
                {overridden ? <span className="vf-live-more"> · some repos differ</span> : null}
              </span>
            ) : null}
            <button type="button" id="verify-close" onClick={closeDialog}>
              Close
            </button>
          </div>

          <div id="verify-body">
            {/* INTAKE'S ANATOMY, in Intake's order: a sentence saying what the
                automation does, the master switch, the SOURCES it watches, the
                work those sources produced, and a fold at the bottom for what
                you touch once. PullRequestsTab and IssuesTab are laid out
                exactly this way, down to the class names — this is the fourth
                surface of the same kind and it should not have to be learned
                separately. */}
            <p className="set-hint set-block-hint">
              MindFlock writes a checklist for every session branch pushed in the
              repositories below, and brings it here to be checked once that work
              reaches the branch you ship from. An agent settles the steps a shell can
              settle; the rest are yours.
            </p>
            <VerifySwitch />
            <VerifySources liveBranch={liveBranch} deployDelay={deployDelay} />
            <WorkListPanel
              label="Checklists"
              onRefresh={() => void plansQ.refetch()}
              note={note}
              rowId="vf-plans-row"
              refreshId="vf-plans-refresh"
              noteId="vf-plans-note"
              listId="vf-plans-list"
              // FIND ONE, OR PICK SEVERAL. This list grows without bound — one
              // checklist per session branch per repo — and until now the only
              // way to a particular one was scrolling. The filter is Recently
              // closed's, down to Ctrl+F and the token rule; the select-all box
              // beside it applies to what the filter is SHOWING, which is what
              // makes "find the sitecheck ones, run them all" a two-gesture job.
              toolbarExtra={
                plans.length ? (
                  <>
                    <SelectAllCheck
                      state={sel.allState}
                      onChange={sel.setAllVisible}
                      label="Select every checklist shown"
                    />
                    <DialogFilter
                      id="vf-plans-filter"
                      value={query}
                      onChange={setQuery}
                      placeholder="Filter by ticket, branch, repo, or what a step says…  ( Ctrl+F )"
                      onEscape={closeDialog}
                    />
                  </>
                ) : undefined
              }
              // Dropped entirely when there is nothing to read it against: a
              // line teaching what a step's lane means, printed over a list with
              // no steps in it, is vocabulary a first-time reader has nowhere to
              // spend.
              hint={
                plans.length ? (
                  <>
                    Steps marked <strong>you</strong> are the job — an agent settles the rest.
                  </>
                ) : undefined
              }
            >
              {error ? (
                <div className="repo-empty">{error}</div>
              ) : !plansQ.data ? null : !plans.length ? (
                <VerifyEmpty liveBranch={liveBranch} />
              ) : (
                <>
                  {picked.length ? (
                    <BulkRowBar
                      count={picked.length}
                      hiddenCount={sel.hiddenCount}
                      noun="checklist"
                      onClear={sel.clear}
                    >
                      <button
                        type="button"
                        disabled={!runnable.length || bulk !== ""}
                        title={
                          runnable.length
                            ? "Start a verify session for each — minutes of a real agent apiece"
                            : "None of the selected checklists is one an agent can run"
                        }
                        onClick={() =>
                          void fanOut(runnable, "Started", (plan) =>
                            api(planPath(plan.id) + "/run", { method: "POST", json: {} }),
                          )
                        }
                      >
                        {/* The NUMBER, always: this is the one control here
                            that spends a workspace and minutes of a billed
                            agent per row, so "Run selected" over eight ticked
                            boxes is not an informed press. Delete asks for
                            confirmation instead, because it cannot be undone;
                            a run can be cancelled from the row it starts. */}
                        {bulk === "Started"
                          ? "Starting…"
                          : "Run " +
                            runnable.length +
                            (runnable.length === picked.length
                              ? ""
                              : " of " + picked.length)}
                      </button>
                      <button
                        type="button"
                        className="danger"
                        disabled={bulk !== ""}
                        title="Delete the selected checklists and every answer recorded against them"
                        onClick={() => setConfirmBulk(true)}
                      >
                        {bulk === "Deleted" ? "Deleting…" : "Delete selected"}
                      </button>
                    </BulkRowBar>
                  ) : null}
                  {confirmBulk ? (
                    // Inline, not a native confirm(): this dialog is
                    // `aria-modal`, and a second window the app did not draw
                    // cannot say what is about to be destroyed. `previewList` is
                    // the same summary Recently closed shows before a wipe.
                    <ConfirmBox
                      title={
                        "Delete " +
                        picked.length +
                        (picked.length === 1 ? " checklist?" : " checklists?")
                      }
                      body={
                        "Their steps and every answer recorded against them go too. " +
                        "This cannot be undone.\n" +
                        previewList(picked.map(planName))
                      }
                      confirmLabel={"Delete " + picked.length}
                      busy={bulk !== ""}
                      onCancel={() => setConfirmBulk(false)}
                      onConfirm={() =>
                        void fanOut(picked, "Deleted", (plan) =>
                          api(planPath(plan.id), { method: "DELETE" }),
                        )
                      }
                    />
                  ) : null}
                  {shown.length ? (
                    <PlanList plans={shown} liveBranch={liveBranch} selection={sel} />
                  ) : (
                    <p className="muted vf-nomatch">
                      No checklist matches “{query}”.
                    </p>
                  )}
                </>
              )}
            </WorkListPanel>
            <VerifyByHand
              candidates={targets}
              closed={closedNames}
              reason={noTargetsReason(instancesQ.data || [], plans, closed)}
            />
          </div>
        </div>
      </div>
    </SettingsCtx.Provider>
  );
}
