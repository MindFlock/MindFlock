/** The step half of a Verify checklist: one step's row (the three answers, the
 * receipt, the note), the in-place step editor, the "add a step of your own"
 * composer and the rewrite box — plus the scratch UI that has to outlive a row
 * being re-parented under another group heading (`NOTE_DRAFTS`, `ROW_UI`).
 *
 * Split out of VerifyDialog.tsx; the rules these render live in verify.ts and
 * the requests they send in lib/verifyActions.ts. */

import { useEffect, useRef, useState } from "react";
import type {
  TestPlan,
  TestStep,
  TestStepActor,
  TestStepResult,
  TestStepResultEntry,
} from "../../api/types";
import { errorPop } from "../../lib/errorPop";
import { errMsg, relTime } from "../../lib/format";
import { addPlanStep } from "../../lib/verifyActions";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import {
  CHECK_MARK,
  asksHumanSteps,
  type CheckState,
  handedBack,
  isYourAnswer,
  noteDraftAfter,
  rewriteBlockedReason,
  rewriteWarning,
  stepCheck,
  stepIsYours,
  stepKeyAction,
  stepKeyAllowed,
  stepKeyIsUndo,
  stepResult,
} from "./verify";

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
  yours: "Needs you",
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
export function byLabel(by: string): string {
  return by === "human" ? "you" : by || "agent";
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

export function rowUi(planId: string): RowUi {
  return ROW_UI.get(planId) || {};
}

export function setRowUi(planId: string, patch: RowUi): void {
  ROW_UI.set(planId, { ...rowUi(planId), ...patch });
}

export function forgetNoteDrafts(): void {
  NOTE_DRAFTS.clear();
  ROW_UI.clear();
}

/** Forget one plan's scratch UI — a deleted checklist's title can come back
 * on the NEXT checklist of that name. */
export function forgetRowUi(planId: string): void {
  ROW_UI.delete(planId);
}

export function StepRow({
  step,
  index,
  plan,
  onRecord,
  onRunStep,
  running,
  onRemove,
  onEdit,
  onFix,
  fixing,
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
  /** Open a session to fix this failed step. Absent while nothing can start
   * one (a run in flight, or another action busy on the plan). */
  onFix?: (step: TestStep) => void;
  /** This row's own fix is starting. */
  fixing?: boolean;
}) {
  const [pending, setPending] = useState<TestStepResult | null>(null);
  // The answer you just gave, until the plan's own copy says the same. Without
  // it the row reads the PREVIOUS answer for the moment between the POST
  // returning and the list catching up — long enough for the note box that a
  // Fail opens to say "pick an answer" about the answer you had just picked.
  const [justGiven, setJustGiven] = useState<TestStepResult | null>(null);
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
  const caughtUp = justGiven !== null && (entry?.result ?? "") === justGiven;
  const given = caughtUp ? null : justGiven;
  // Once the list agrees, forget it — a later change (an agent's re-check, an
  // answer from another window) must show through.
  useEffect(() => {
    if (caughtUp) setJustGiven(null);
  }, [caughtUp]);
  const shown = pending ?? given ?? entry?.result ?? "";
  // Who the answer on screen belongs to. While a POST is in flight the answer
  // being shown is the one YOU just gave, whatever the stored entry says — so a
  // Can't check settles the row instantly rather than un-highlighting a beat
  // later when the server's copy arrives.
  const shownBy = pending !== null || given !== null ? "human" : entry?.by || "";
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
  const mayResave = answered && (given !== null || !entry || entry.by === "human");

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
      setJustGiven(result);
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
      label: running ? "Checking…" : "Check this step again",
      title: "An agent checks only this step",
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
      title: "Change the wording — clears any answer on it",
      onSelect: () => setEditing(true),
    });
    stepMenu.push({
      key: "actor",
      // The flip is worth its own item rather than living inside the editor: it
      // is the only way out of a checklist an agent cannot run at all (an
      // unrecognised actor is coerced to "human", and the run route refuses a
      // plan where every step is a person's), and it costs no answers.
      label: mine ? "Let the agent check this" : "I'll check this one",
      title: mine ? "The agent checks it next time" : "The agent leaves it for you",
      onSelect: () =>
        void onEdit(step, { actor: mine ? "agent" : "human" }),
    });
  }
  if (step.manual && onRemove)
    stepMenu.push({
      key: "remove",
      label: "Remove this step",
      title: "Delete this step for good",
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
        data-tour="verify-badges"
        title={
          handed
            ? "The agent couldn't check this one — it's yours now"
            : mine
              ? "Only a person can check this"
              : "The agent checks this — you can still answer it yourself"
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
              gets marked pass to make the row go away. One span, so the tour
              can ring the three together and a narrow row can stack the
              receipt under them. */}
          <span className="vf-answers" data-tour="verify-answer">
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
          </span>
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
                  {/* The age, in the one formatter's words with an age suffix ("3h old"). */}
                  {entry.at ? " · " + relTime(entry.at, { suffix: " old" }) : ""}
                </span>
                <button
                  type="button"
                  className="vf-undo"
                  disabled={pending !== null || saving}
                  title="Take this answer back"
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
            {/* THE NEXT MOVE ON A FAILED STEP, beside it rather than in the
                checklist's ⋯: open a session to fix it. The session is told to
                reproduce the failure first. */}
            {shown === "fail" && onFix ? (
              <button
                type="button"
                className="vf-fix-step"
                disabled={fixing}
                title="Starts a new session with this failure attached"
                onClick={() => onFix(step)}
              >
                {fixing ? "Starting…" : "Fix in a new session"}
              </button>
            ) : null}
          </span>
          {stepMenu.length ? (
            <OverflowMenu
              label={"More for step " + (index + 1)}
              items={stepMenu}
              boundaryId="verify-body"
            />
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
              <span className="vf-note-hint">
                Pick Pass, Fail or Can't check to save it
              </span>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

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
export function RewriteBox({
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
export function AddStep({ plan, onAdded }: { plan: TestPlan; onAdded(next: TestPlan): void }) {
  const [text, setText] = useState("");
  const [expect, setExpect] = useState("");
  const [actor, setActor] = useState<TestStepActor>("agent");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    const body = text.trim();
    if (!body) return;
    setBusy(true);
    try {
      const next = await addPlanStep(plan, { text: body, expect: expect.trim(), actor });
      if (next) onAdded(next);
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
        <option value="agent">Agent checks it</option>
        <option value="human">You check it</option>
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
