/** The Verify card: a session's verify, opened in place on the Sky (the
 * Verify tile, a Verify stop, a board row).
 *
 * VERIFY IS THE SESSION'S OWN AGENT (owner request 2026-11-10): "Verify"
 * has it write its own actionable steps and work them in its terminal, and
 * tell the person whatever only a person can do. So the card is small: the
 * status sentence, ONE primary action (Verify, or Open its agent while it
 * works), ⋯ (the rest, in a portal — the card sits on a zoomed canvas), ×;
 * Confirm verified / Not verified… when it waits on you ("Sign off" when no
 * step is a person's); then the steps, read-only and listed ONCE, each with
 * who does it, its result, who answered and the evidence. A failed verify's
 * primary is "Let its agent fix it" (`onFixInSession`; "Fix in a new
 * session" stays in ⋯). A Verify stop that HALTED its route keeps Retry ·
 * Skip · Let the agent fix it. */

import { useRef, useState, type JSX } from "react";
import { createPortal } from "react-dom";
import type { TestPlan, TestStep } from "../../api/types";
import { behaviorSummary, planForSession, tallyLine } from "../../lib/behaviors";
import { flightAction, flightPath, stepRetry, stepSkip } from "../../lib/flightsApi";
import { verifyOf } from "../../lib/sessionState";
import * as actions from "../../lib/verifyActions";
import { useFlights, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { StopMenu, type SkyMenuItem } from "../flights/StopMenu";
import { InlineConfirm } from "../InlineConfirm";
import { BehaviorRow } from "./BehaviorRow";
import { cardMenuItems, haltedLine, verifyCardView, verifyStopStand, watchSessionOf, type CardMenuItem, type CardMenuKey } from "./cardModel";

/** The card's default "Let its agent fix it": today the existing fix (a new
 * session with what failed attached). ORCHESTRATOR: once
 * lib/verifyActions.ts has `fixInSession(planId)`, make this
 * `actions.fixInSession(plan.id)` — the one place the card's fix is wired
 * (the head's primary and every failed row's button both call it). */
export function defaultFixInSession(plan: TestPlan, _stepIds?: string[]): Promise<unknown> {
  // The server hands EVERY failed step, with its evidence, to the session's
  // own agent and verifies again after its turn; when that session is gone it
  // answers without `in_session` and `fixInSession` says a new one was opened.
  return actions.fixInSession(plan.id, plan.title || plan.id);
}
import { FailedSteps, VerifyConfirm } from "./VerifyConfirm";
import "./BehaviorsCard.css";

export interface BehaviorsCardProps {
  plan: TestPlan;
  /** × / Esc: fold the card back into its tile or row. */
  onClose?(): void;
  /** Narrow layout (a board row, a small tile slot). */
  compact?: boolean;
  /** Start with every step open (they start collapsed to one line). */
  expanded?: boolean;
  /** Set when the card is a Verify stop's: the route it parks. Lets the card
   * offer "Skip this stop" — and, when the stop's steps failed and HALTED
   * the route, Retry · Skip · Let the agent fix it. */
  flightStep?: { flightId: string; stepId: string } | null;
  /** "Let its agent fix it" (the failed head's primary, a failed row's
   * button): hand what failed to the session's own agent. Default
   * `defaultFixInSession`. */
  onFixInSession?(plan: TestPlan, stepIds?: string[]): Promise<unknown>;
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

export function BehaviorsCard({ plan, onClose, compact, flightStep, expanded, onFixInSession = defaultFixInSession }: BehaviorsCardProps): JSX.Element {
  const { data: instances } = useInstances();
  const session = (instances || []).find((i) => planForSession([plan], i) === plan) || null;
  const verify = session ? verifyOf(session) : null;
  const s = verifyCardView(plan, behaviorSummary(plan), verify, session?.title || "");
  const steps = plan.steps || [];
  const tally = tallyLine(plan);
  const running = plan.state === "running";
  const watch = watchSessionOf(plan);
  // The Verify stop this card stands in for, if any: halted on a failure, or
  // parked on you (docs/behaviors.md §5).
  const { data: flights } = useFlights(!!flightStep);
  const flight = flightStep ? (flights || []).find((f) => f.id === flightStep.flightId) || null : null;
  const stand = flightStep ? verifyStopStand(flight, flightStep.stepId) : "";

  const [busy, setBusy] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  // Rows start collapsed (one short line each — BehaviorRow); this opens or
  // closes them all at once.
  const [allOpen, setAllOpen] = useState<{ open: boolean; n: number }>({ open: !!expanded, n: 0 });
  const moreRef = useRef<HTMLButtonElement>(null);

  const withBusy = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try {
      await fn();
    } finally {
      setBusy("");
    }
  };

  /** Verify: the session's own agent, in its terminal (its Agent tab opens).
   * With the session gone, the plan's own run (`/run`) — an older path the
   * server still answers. */
  const verifyNow = () =>
    void withBusy("primary", () => (session ? actions.startSessionVerify(session.title) : actions.startChecklistRun(plan)));

  const primary = () => {
    const a = s.action;
    if (!a) return;
    if (a.key === "open" && session) actions.openWalkingSession(session.title);
    else if (a.key === "verify") verifyNow();
    else if (a.key === "agentfix") void withBusy("primary", () => onFixInSession(plan));
  };

  const pick = (key: CardMenuKey) => {
    const ui = useUi.getState();
    switch (key) {
      case "verify":
        verifyNow();
        break;
      case "deployed":
        void withBusy("deployed", () => actions.markPlanDeployed(plan));
        break;
      case "fix":
        void withBusy("fix", () => actions.fixPlanFailures(plan));
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

  const items: CardMenuItem[] = cardMenuItems(plan, { summary: s, session: session ? { title: session.title, pr_url: session.pr_url, verify } : null });
  const fixOne = running || busy ? undefined : (step: TestStep) => void withBusy("fix", () => onFixInSession(plan, [step.id]));

  return (
    <section
      className={"bh-card is-" + s.tone + (compact ? " is-compact" : "")}
      data-plan={plan.id}
      data-tone={s.tone}
      data-tour="behaviors-card"
      data-no-drag
      aria-label={"Verify — " + s.status}
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
          <span className="bh-title">Verify</span>
          <span className="bh-status" data-tone={s.tone}>
            {s.status}
          </span>
          {tally ? (
            <span className="bh-count bh-tally" title="Where its steps stand">
              {tally}
            </span>
          ) : null}
          {steps.length > 1 ? (
            <button
              type="button"
              className="tb-item bh-quiet bh-expand-all"
              title={allOpen.open ? "Collapse every step to one line" : "Show every step in full"}
              onClick={() => setAllOpen((a) => ({ open: !a.open, n: a.n + 1 }))}
            >
              {allOpen.open ? "Collapse all" : "Expand all"}
            </button>
          ) : null}
          <span className="bh-head-tools">
            <button
              ref={moreRef}
              type="button"
              className="fl-icon-btn bh-more"
              aria-label="More for Verify"
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
        <p className="bh-sentence">{stand === "halted" ? haltedLine(s.failed) : s.sentence}</p>
        {stand === "halted" && flightStep ? (
          // A failed step HALTS the route (the owner likes halt; no
          // recovery ladder): Retry verifies again, Skip moves on without it,
          // "Let the agent fix it" hands the failure to the session's agent
          // and checks again once its turn ends (POST …/op/fix).
          <div className="bh-head-actions bh-halt-actions">
            <button
              type="button"
              className="btn-primary bh-retry"
              title="Verify again"
              disabled={!!busy}
              onClick={() => void withBusy("retry", () => stepRetry(flightStep.flightId, flightStep.stepId))}
            >
              {busy === "retry" ? "…" : "Retry"}
            </button>
            <button
              type="button"
              className="test-btn bh-skip"
              title="The route moves on without this Verify"
              disabled={!!busy}
              onClick={() => void withBusy("skip", () => stepSkip(flightStep.flightId, flightStep.stepId))}
            >
              {busy === "skip" ? "…" : "Skip"}
            </button>
            <button
              type="button"
              className="test-btn bh-fix-agent"
              title="Hand what failed to this session's agent; it verifies again once the fix is done"
              disabled={!!busy}
              onClick={() =>
                void withBusy("agentfix", () =>
                  flightAction("Let the agent fix it", flightPath(flightStep.flightId, "/steps/" + encodeURIComponent(flightStep.stepId) + "/op/fix"), {}, "Handed to the agent — it verifies again after its fix")
                )
              }
            >
              {busy === "agentfix" ? "…" : "Let the agent fix it"}
            </button>
          </div>
        ) : s.action || flightStep ? (
          <div className="bh-head-actions">
            {s.action ? (
              <button type="button" className="btn-primary bh-primary" data-action={s.action.key} title={s.action.title} disabled={!!busy} onClick={primary}>
                {busy === "primary" ? "…" : s.action.label}
              </button>
            ) : null}
            {flightStep && stand !== "fixing" ? (
              <button
                type="button"
                className="test-btn bh-skip"
                title="The route moves on without this Verify"
                disabled={!!busy}
                onClick={() => void withBusy("skip", () => stepSkip(flightStep.flightId, flightStep.stepId))}
              >
                {busy === "skip" ? "…" : "Skip this stop"}
              </button>
            ) : null}
          </div>
        ) : null}
      </header>

      {s.confirm && verify ? (
        // The steps are listed once, in the rows below — the confirm is only
        // the answer. With no person steps it is a sign-off.
        <div className="bh-confirm">
          {s.signOff ? <div className="bh-signoff">Sign off</div> : null}
          <VerifyConfirm verify={verify} hideSteps />
        </div>
      ) : null}
      <FailedSteps steps={s.failedSteps} />

      {confirmDelete ? (
        <InlineConfirm
          title="Delete these steps?"
          body={"Their results go too" + (running ? ", and the Verify under way stops." : ".")}
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
            <BehaviorRow key={st.id} plan={plan} step={st} index={i} onFix={fixOne} allOpen={allOpen} />
          ))}
        </ol>
      ) : null}

      {menu && typeof document !== "undefined"
        ? createPortal(
            <div className="sky-chain-menu" style={{ position: "fixed", left: 0, top: 0, width: 0, height: 0, zIndex: 80, ["--sky-z" as string]: 1 }}>
              <StopMenu
                x={menu.x}
                y={menu.y}
                label="Verify — what you can do"
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
