/** The Behaviors card: a session's verify, opened in place on the Sky (the
 * Behaviors tile, a Verify stop, a Behaviors-board row).
 *
 * VERIFY IS THE SESSION'S OWN AGENT (owner request 2026-11-10): "Verify"
 * has it write its own actionable steps and work them in its terminal, and
 * tell the person whatever only a person can do. So the card is small: the
 * status sentence, ONE primary action (Verify, or Open its agent while it
 * works), ⋯ (the rest, in a portal — the card sits on a zoomed canvas), ×;
 * the steps only a person can do with Confirm verified / Not verified… when
 * it waits on you; then the agent's steps, read-only, each with who does it
 * and its result. A Verify stop that HALTED its route keeps Retry · Skip ·
 * Let the agent fix it. */

import { useRef, useState, type JSX } from "react";
import { createPortal } from "react-dom";
import type { TestPlan, TestStep } from "../../api/types";
import { behaviorSummary, behaviorsCount, planForSession } from "../../lib/behaviors";
import { flightAction, flightPath, stepRetry, stepSkip } from "../../lib/flightsApi";
import { verifyOf } from "../../lib/sessionState";
import * as actions from "../../lib/verifyActions";
import { useFlights, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { StopMenu, type SkyMenuItem } from "../flights/StopMenu";
import { InlineConfirm } from "../InlineConfirm";
import { BehaviorRow } from "./BehaviorRow";
import { cardMenuItems, haltedLine, verifyCardView, verifyStopStand, watchSessionOf, type CardMenuItem, type CardMenuKey } from "./cardModel";
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
   * offer "Skip this stop" — and, when the stop's behaviors failed and HALTED
   * the route, Retry · Skip · Let the agent fix it. */
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

export function BehaviorsCard({ plan, onClose, compact, flightStep, expanded }: BehaviorsCardProps): JSX.Element {
  const { data: instances } = useInstances();
  const session = (instances || []).find((i) => planForSession([plan], i) === plan) || null;
  const verify = session ? verifyOf(session) : null;
  const s = verifyCardView(plan, behaviorSummary(plan), verify, session?.title || "");
  const steps = plan.steps || [];
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
   * With the session gone, the behaviors' own run (`/run`) — an older path
   * the server still answers. */
  const verifyNow = () =>
    void withBusy("primary", () => (session ? actions.startSessionVerify(session.title) : actions.startChecklistRun(plan)));

  const primary = () => {
    const a = s.action;
    if (!a) return;
    if (a.key === "open" && session) actions.openWalkingSession(session.title);
    else if (a.key === "verify") verifyNow();
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
  const fixOne = running || busy ? undefined : (step: TestStep) => void withBusy("fix", () => actions.fixPlanFailures(plan, [step.id]));

  return (
    <section
      className={"bh-card is-" + s.tone + (compact ? " is-compact" : "")}
      data-plan={plan.id}
      data-tone={s.tone}
      data-tour="behaviors-card"
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
          {steps.length ? <span className="bh-count">{behaviorsCount(steps.length)}</span> : null}
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
        <p className="bh-sentence">{stand === "halted" ? haltedLine(s.failed) : s.sentence}</p>
        {stand === "halted" && flightStep ? (
          // A failed behavior HALTS the route (the owner likes halt; no
          // recovery ladder): Retry checks again, Skip moves on without them,
          // "Let the agent fix it" hands the failure to the session's agent
          // and checks again once its turn ends (POST …/op/fix).
          <div className="bh-head-actions bh-halt-actions">
            <button
              type="button"
              className="btn-primary bh-retry"
              title="Check these behaviors again"
              disabled={!!busy}
              onClick={() => void withBusy("retry", () => stepRetry(flightStep.flightId, flightStep.stepId))}
            >
              {busy === "retry" ? "…" : "Retry"}
            </button>
            <button
              type="button"
              className="test-btn bh-skip"
              title="The route moves on without these behaviors"
              disabled={!!busy}
              onClick={() => void withBusy("skip", () => stepSkip(flightStep.flightId, flightStep.stepId))}
            >
              {busy === "skip" ? "…" : "Skip"}
            </button>
            <button
              type="button"
              className="test-btn bh-fix-agent"
              title="Hand what failed to this session's agent; the behaviors are checked again once it is done"
              disabled={!!busy}
              onClick={() =>
                void withBusy("agentfix", () =>
                  flightAction("Let the agent fix it", flightPath(flightStep.flightId, "/steps/" + encodeURIComponent(flightStep.stepId) + "/op/fix"), {}, "Handed to the agent — the behaviors are checked again after its fix")
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
                title="The route moves on without this check"
                disabled={!!busy}
                onClick={() => void withBusy("skip", () => stepSkip(flightStep.flightId, flightStep.stepId))}
              >
                {busy === "skip" ? "…" : "Skip this stop"}
              </button>
            ) : null}
          </div>
        ) : null}
      </header>

      {s.confirm && verify ? <VerifyConfirm verify={verify} /> : null}
      <FailedSteps steps={s.failedSteps} />

      {confirmDelete ? (
        <InlineConfirm
          title="Delete these behaviors?"
          body={"Their results go too" + (running ? ", and the check running now stops." : ".")}
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
