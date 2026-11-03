/** The Behaviors card (docs/behaviors.md §2): the ONE place a session's
 * behaviors are read, answered and edited — opened in place on the Sky
 * (the Behaviors tile, a Verify stop, a Behaviors-board row).
 *
 * SEAM (pre-committed by the integrator). `BehaviorsCardProps` is frozen; the
 * body is vs-card's. Head: the status sentence, ONE primary action, ⋯ (the
 * rest, in a portal — the card sits on a zoomed canvas), ×. Body: one
 * BehaviorRow per behavior. Foot: "+ Add a behavior". No blanket Done: a
 * Verify stop parked on you moves on when the behaviors are answered (the
 * server's `session.test_plan_settled`), or on Skip. */

import { useRef, useState, type JSX, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import type { TestPlan, TestStep, TestStepActor, TestStepResult } from "../../api/types";
import { behaviorSummary, behaviorsCount } from "../../lib/behaviors";
import { stepSkip } from "../../lib/flightsApi";
import { errorPop } from "../../lib/errorPop";
import { errMsg } from "../../lib/format";
import * as actions from "../../lib/verifyActions";
import { refreshTestPlans, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { planForSession } from "../../lib/behaviors";
import { rewriteBlockedReason, rewriteWarning } from "../dialogs/verify";
import { StopMenu, type SkyMenuItem } from "../flights/StopMenu";
import { InlineConfirm } from "../InlineConfirm";
import { BehaviorRow } from "./BehaviorRow";
import { cardMenuItems, primaryTarget, watchSessionOf, type CardMenuItem, type CardMenuKey } from "./cardModel";
import "./BehaviorsCard.css";

export interface BehaviorsCardProps {
  plan: TestPlan;
  /** × / Esc: fold the card back into its tile or row. */
  onClose?(): void;
  /** Narrow layout (a board row, a small tile slot). */
  compact?: boolean;
  /** Set when the card is a Verify stop's: the chain it parks. Lets the card
   * offer "Skip this stop" next to the behaviors. */
  flightStep?: { flightId: string; stepId: string } | null;
}

/** The ✓-shield the tile wears too. */
export function ShieldIcon(): JSX.Element {
  return (
    <svg className="bh-shield" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      <path d="M8 1.5 13.5 3.5v4c0 3.3-2.3 5.9-5.5 7-3.2-1.1-5.5-3.7-5.5-7v-4z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="m5.4 8.1 1.9 1.9 3.4-3.6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function BehaviorsCard({ plan, onClose, compact, flightStep }: BehaviorsCardProps): JSX.Element {
  const s = behaviorSummary(plan);
  const { data: instances } = useInstances();
  const session = (instances || []).find((i) => planForSession([plan], i) === plan) || null;
  const steps = plan.steps || [];
  const running = plan.state === "running";
  const watch = watchSessionOf(plan);

  const [busy, setBusy] = useState("");
  const [rewriting, setRewriting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [adding, setAdding] = useState(false);
  const [focus, setFocus] = useState<{ id: string; n: number }>({ id: "", n: 0 });
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const moreRef = useRef<HTMLButtonElement>(null);

  const withBusy = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try {
      await fn();
    } finally {
      setBusy("");
    }
  };
  const focusRow = (id: string) => id && setFocus((f) => ({ id, n: f.n + 1 }));

  const primary = () => {
    const a = s.action;
    if (!a) return;
    if (a.key === "check" || a.key === "rerun") void withBusy("primary", () => actions.startChecklistRun(plan));
    else if (a.key === "failed" || a.key === "answer") focusRow(primaryTarget(plan, a.key));
    else if (a.key === "write") {
      // Empty on purpose → a person writes them; otherwise the agent tries again.
      if (plan.origin === "manual") setAdding(true);
      else void withBusy("primary", () => actions.regeneratePlan(plan));
    }
  };

  const pick = (key: CardMenuKey) => {
    const ui = useUi.getState();
    switch (key) {
      case "rewrite":
        setRewriting(true);
        break;
      case "deployed":
        void withBusy("deployed", () => actions.markPlanDeployed(plan));
        break;
      case "fix":
        void withBusy("fix", () => actions.fixPlanFailures(plan));
        break;
      case "run":
        void withBusy("primary", () => actions.startChecklistRun(plan));
        break;
      case "watch":
        // The checking run is a grid window (read-only "Verifying" pane).
        ui.openVerifyPane(watch);
        ui.setView("grid");
        break;
      case "cancel":
        void withBusy("cancel", () => actions.cancelChecklistRun(plan));
        break;
      case "session":
        if (session) ui.showSky(session.title);
        break;
      case "diff":
        if (session) {
          ui.openDiff(session.title);
          ui.setView("grid");
        }
        break;
      case "risk":
        if (session) {
          ui.openMap(session.title, { section: "risk" });
          ui.setView("grid");
        }
        break;
      case "pr":
        if (session?.pr_url) window.open(session.pr_url, "_blank");
        break;
      case "delete":
        setConfirmDelete(true);
        break;
    }
  };

  const items: CardMenuItem[] = cardMenuItems(plan, { summary: s, session });

  const record = (step: TestStep, result: TestStepResult, note: string) => actions.recordStepAnswer(plan, step, result, note);
  const edit = running ? undefined : (step: TestStep, fields: Partial<TestStep>) => actions.editPlanStep(plan, step, fields);
  const remove = running ? undefined : (step: TestStep) => actions.removePlanStep(plan, step);
  const fixOne = running || busy ? undefined : (step: TestStep) => void withBusy("fix", () => actions.fixPlanFailures(plan, [step.id]));

  return (
    <section
      className={"bh-card is-" + s.tone + (compact ? " is-compact" : "")}
      data-plan={plan.id}
      data-tone={s.tone}
      data-no-drag
      aria-label={"Behaviors — " + s.status}
      onKeyDown={(e) => {
        if (e.key !== "Escape" || !onClose || menu) return;
        const from = e.target as HTMLElement;
        if (from.closest("input, textarea, select")) return;
        e.stopPropagation();
        e.preventDefault();
        onClose();
      }}
    >
      <header className="bh-head">
        <div className="bh-head-row">
          <ShieldIcon />
          <span className="bh-title">Behaviors</span>
          <span className="bh-status" data-tone={s.tone}>
            {s.status}
          </span>
          {s.total ? <span className="bh-count">{behaviorsCount(s.total)}</span> : null}
          <span className="bh-head-tools">
            <button
              ref={moreRef}
              type="button"
              className="fl-icon-btn bh-more"
              aria-label="More for these behaviors"
              aria-haspopup="menu"
              aria-expanded={!!menu}
              onClick={(e) => {
                const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
                setMenu(menu ? null : { x: Math.max(8, r.right - 220), y: r.bottom + 4 });
              }}
            >
              ⋯
            </button>
            {onClose ? (
              <button type="button" className="fl-icon-btn bh-close" aria-label="Close" title="Fold back (Esc)" onClick={onClose}>
                ×
              </button>
            ) : null}
          </span>
        </div>
        <p className="bh-sentence">{s.sentence}</p>
        {s.action || flightStep ? (
          <div className="bh-head-actions">
            {s.action ? (
              <button type="button" className="btn-primary bh-primary" data-action={s.action.key} disabled={!!busy} onClick={primary}>
                {busy === "primary" ? "…" : s.action.label}
              </button>
            ) : null}
            {flightStep ? (
              <button
                type="button"
                className="test-btn bh-skip"
                title="The chain moves on without these answers"
                disabled={!!busy}
                onClick={() => void withBusy("skip", () => stepSkip(flightStep.flightId, flightStep.stepId))}
              >
                {busy === "skip" ? "…" : "Skip this stop"}
              </button>
            ) : null}
          </div>
        ) : null}
      </header>

      {rewriting ? <RewriteBehaviors plan={plan} onDone={() => setRewriting(false)} /> : null}
      {confirmDelete ? (
        <InlineConfirm
          title="Delete these behaviors?"
          body={"Their answers go too" + (running ? ", and the check running now stops." : ".")}
          confirmLabel="Delete"
          cancelLabel="Keep them"
          busy={busy === "delete"}
          onConfirm={() =>
            void withBusy("delete", async () => {
              await actions.removePlan(plan, { watchSession: watch });
              setConfirmDelete(false);
              onClose?.();
            })
          }
          onCancel={() => setConfirmDelete(false)}
        />
      ) : null}

      {steps.length ? (
        <ol className="bh-rows sky-own-scroll">
          {steps.map((st, i) => (
            <BehaviorRow
              key={st.id}
              plan={plan}
              step={st}
              index={i}
              onRecord={record}
              onEdit={edit}
              onRemove={remove}
              onFix={fixOne}
              focusNonce={focus.id === st.id ? focus.n : 0}
            />
          ))}
        </ol>
      ) : null}

      <footer className="bh-foot">
        {adding ? (
          <AddBehavior plan={plan} onDone={() => setAdding(false)} />
        ) : (
          <button type="button" className="tb-item bh-add" disabled={plan.state === "generating"} onClick={() => setAdding(true)}>
            + Add a behavior
          </button>
        )}
      </footer>

      {menu && typeof document !== "undefined"
        ? createPortal(
            <div className="sky-chain-menu" style={{ position: "fixed", left: 0, top: 0, width: 0, height: 0, zIndex: 80, ["--sky-z" as string]: 1 }}>
              <StopMenu
                x={menu.x}
                y={menu.y}
                label="Behaviors — what you can do"
                items={items.map((it) => ({ ...it, key: it.key as unknown as SkyMenuItem["key"] }))}
                onPick={(key) => {
                  setMenu(null);
                  pick(key as unknown as CardMenuKey);
                }}
                onClose={(back) => {
                  setMenu(null);
                  if (back) moreRef.current?.focus();
                }}
              />
            </div>,
            document.body
          )
        : null}
    </section>
  );
}

/** "Rewrite behaviors", with the one input that makes the second draft better
 * than the first: what to check instead (optional). */
function RewriteBehaviors({ plan, onDone }: { plan: TestPlan; onDone(): void }): JSX.Element {
  const [focus, setFocus] = useState(plan.focus || "");
  const [busy, setBusy] = useState(false);
  const blocked = rewriteBlockedReason(plan);
  const warning = blocked ? "" : rewriteWarning(plan).replace(/Steps/g, "Behaviors").replace(/steps/g, "behaviors");
  const go = async () => {
    setBusy(true);
    try {
      if (await actions.regeneratePlan(plan, focus)) onDone();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="bh-rewrite">
      <label className="bh-rewrite-title" htmlFor={"bh-rewrite-" + plan.id}>
        Rewrite behaviors
      </label>
      <textarea
        id={"bh-rewrite-" + plan.id}
        className="fl-input"
        rows={2}
        autoFocus
        placeholder="What should they check instead? (optional) — e.g. “focus on the coupon flow at checkout”"
        value={focus}
        disabled={busy || !!blocked}
        onChange={(e) => setFocus(e.target.value)}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Escape") {
            e.preventDefault();
            onDone();
          }
        }}
      />
      {blocked || warning ? <p className="bh-hint">{blocked || warning}</p> : null}
      <div className="bh-edit-actions">
        <button type="button" className="btn-primary" disabled={busy || !!blocked} onClick={() => void go()}>
          {busy ? "Asking…" : "Rewrite"}
        </button>
        <button type="button" className="test-btn" disabled={busy} onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/** "+ Add a behavior", inline at the foot: what the work should do, what
 * should happen, and who checks it. Enter adds and keeps the form open for
 * the next one; Esc closes it. */
export function AddBehavior({ plan, onDone }: { plan: TestPlan; onDone(): void }): JSX.Element {
  const [text, setText] = useState("");
  const [expect, setExpect] = useState("");
  const [actor, setActor] = useState<TestStepActor>("agent");
  const [busy, setBusy] = useState(false);
  const textRef = useRef<HTMLInputElement>(null);
  const add = async () => {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    try {
      await addBehavior(plan, { text: body, expect: expect.trim(), actor });
      setText("");
      setExpect("");
      textRef.current?.focus();
    } catch (err) {
      errorPop("Couldn't add that behavior", errMsg(err));
    } finally {
      setBusy(false);
    }
  };
  const keys = (e: ReactKeyboardEvent) => {
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      void add();
    } else if (e.key === "Escape") {
      e.preventDefault();
      onDone();
    }
  };
  return (
    <div className="bh-addform" role="group" aria-label="Add a behavior">
      <input ref={textRef} type="text" className="fl-input" autoFocus autoComplete="off" placeholder="what the work should do" aria-label="New behavior" value={text} disabled={busy} onChange={(e) => setText(e.target.value)} onKeyDown={keys} />
      <input type="text" className="fl-input" autoComplete="off" placeholder="what should happen (optional)" aria-label="What should happen" value={expect} disabled={busy} onChange={(e) => setExpect(e.target.value)} onKeyDown={keys} />
      <div className="bh-edit-actions">
        <button
          type="button"
          className={"bh-who" + (actor === "human" ? " is-you" : "")}
          aria-pressed={actor === "human"}
          title="Who checks it — press to switch"
          disabled={busy}
          onClick={() => setActor(actor === "human" ? "agent" : "human")}
        >
          {actor === "human" ? "🙋 you" : "🤖 agent"}
        </button>
        <button type="button" className="btn-primary" disabled={busy || !text.trim()} onClick={() => void add()}>
          {busy ? "Adding…" : "Add"}
        </button>
        <button type="button" className="test-btn" disabled={busy} onClick={onDone}>
          Done adding
        </button>
      </div>
    </div>
  );
}

/** Add one behavior and apply the server's copy of the plan to the cache.
 * Rejects with the server's message (the form keeps its text then). */
export async function addBehavior(plan: TestPlan, fields: { text: string; expect: string; actor: TestStepActor }): Promise<void> {
  const next = await actions.addPlanStep(plan, fields);
  if (next) actions.patchPlan(plan.id, () => next);
  void refreshTestPlans();
}
