/** The sky view: the drawer at the sky's right edge — what the old Board
 * listed, so nothing it showed is lost now that the Sky replaced it:
 *
 *  - Needs you — THE one list (lib/needsYou `useNeedsYou`, the bell's rows in
 *    the bell's order, one count with the bell's badge): a session asking you
 *    something (its answer strip), a route at a Wait for me stop (its
 *    checklist and Done / Skip, the same block the route's stop holds) or
 *    halted (Retry / Skip), approvals and escalations (the bell's waiting
 *    row), a plan waiting for Go, broken / failing / ready sessions, and
 *    behaviors that need you or failed (a row that flies the sky to them —
 *    lib/behaviorsFocus `openBehaviors`). A press on a name flies the sky to
 *    its route.
 *  - Waiting to fly — the tickets, pull requests and issues that are eligible
 *    and have no session, under their own sub-headings, a kind badge on every
 *    row, the saved stop as a chip row and one action: Fly. Folded: the intake
 *    lane at the sky's left edge draws them too.
 *  - Landed — finished flights, folded. */

import { useState } from "react";
import type { TestPlan } from "../../api/types";
import { useFlightKinds, useFlightPlans, useFlights, useInstances, useSettingsDoc } from "../../state/queries";
import { ageText } from "../intake/kit";
import { useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { windowName } from "../../lib/windowName";
import { haltedWhy } from "../../lib/sessionState";
import { isFailing, planName } from "../dialogs/verify";
import { useNeedsYou, type NeedsYouRow } from "../../lib/needsYou";
import { openAsk } from "../../lib/ask";
import { AnswerStrip } from "../AnswerStrip";
import { WaitingRow as OutboxWaitingRow } from "../outbox/WaitingRow";
import { FlightCard, YouStepActions, flightHeadline } from "./FlightStrip";
import { useWaiting, type Waiting } from "./useWaiting";
import {
  WAITING_KIND_BADGE,
  WAITING_KIND_HEAD,
  builtinPlanFor,
  isLive,
  perchLabel,
  planChoices,
  stepActor,
  type Flight,
  type FlightPlan,
  type StepKindDef,
  type WaitingKind,
} from "../../lib/flights";
import { flyWaiting, type WaitingTarget } from "../../lib/flightsApi";
import { ChipRow } from "./controls";
import "./Flights.css";

/** The saved stop a source names for its flights (`TicketingSource.plan`,
 * `github.repo_settings[repo].plan`, `issue_repo_settings[repo].plan`), "" =
 * none (the server then uses the built-in cut at the source's depth). */
export function sourcePlan(settings: Record<string, unknown> | undefined, t: WaitingTarget): string {
  const s = (settings || {}) as {
    ticketing?: { sources?: Array<{ id?: string; plan?: string }> };
    github?: {
      repo_settings?: Record<string, { plan?: string }>;
      issue_repo_settings?: Record<string, { plan?: string }>;
    };
  };
  if (t.kind === "tickets") return (s.ticketing?.sources || []).find((x) => x.id === t.source)?.plan || "";
  const per = t.kind === "prs" ? s.github?.repo_settings : s.github?.issue_repo_settings;
  return per?.[t.repo]?.plan || "";
}

/** Behaviors that need you or failed: one line that flies the sky to them
 * (`onOpen(plan id)` → `openBehaviors({ planId })`). */
export function ChecklistRow({ plan, onOpen }: { plan: TestPlan; onOpen(planId: string): void }) {
  const failing = isFailing(plan);
  return (
    <button
      type="button"
      className={"fl-check-row" + (failing ? " is-failing" : "")}
      title={failing ? "Show the behaviors that failed" : "Show the behaviors that need you"}
      onClick={() => onOpen(plan.id)}
    >
      <span className="fl-check-glyph" aria-hidden="true">{failing ? "✗" : "☐"}</span>
      <span className="fl-check-name">{planName(plan)}</span>
      <span className="fl-check-state">{failing ? "Failed" : "Needs you"}</span>
      {plan.branch && <code className="fl-check-branch">{plan.branch}</code>}
    </button>
  );
}

/** Where an item will go: the repo (a PR's / issue's own, a ticket's routed
 * one when the source knows it). */
function whereTo(w: Waiting): string {
  const it = w.item;
  if (it.kind !== "tickets") return it.group;
  const repos = it.route?.repos || [];
  return repos.length === 1 ? repos[0] : repos.length > 1 ? repos.length + " repos" : "";
}

/** One item waiting to fly: its badge, reference and headline, where it will
 * go, the saved stops as chips, and Fly. */
export function WaitingRow({ w, plans, settings }: { w: Waiting; plans: FlightPlan[]; settings: Record<string, unknown> | undefined }) {
  const t = w.item.target as WaitingTarget;
  // The source's own saved stop (its card's chips) is the default; a pick here
  // overrides it for this flight only. `null` = follow the source, so a
  // settings document that loads after the row still decides.
  const own = sourcePlan(settings, t);
  const [picked, setPicked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const plan = picked ?? own;
  const kind = w.item.kind as WaitingKind;
  const where = whereTo(w);
  const meta = [w.item.kind === "tickets" ? w.item.group : "", w.item.state, ageText(w.item.created_at), w.item.assignee ? "assigned to " + w.item.assignee : ""]
    .filter(Boolean)
    .join(" · ");
  const fly = async () => {
    setBusy(true);
    setError("");
    try {
      // The empty chip is the kind's built-in: sent as such when the source
      // names a saved stop of its own (else the server would use that).
      const send = plan || (own ? builtinPlanFor(kind, plans) : "");
      await flyWaiting(t, send, {}, kind === "tickets" ? w.item.reference : "");
      // Flying opens the session (flyWaiting selects it as soon as its row
      // exists): the dialog steps aside, as a template's Run now does.
      useUi.getState().closeDialog();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={"fl-waiting fl-waiting-" + kind} data-waiting={w.item.key}>
      <div className="fl-waiting-main">
        <span className={"fl-kind-badge fl-kind-badge-" + kind}>{WAITING_KIND_BADGE[kind]}</span>
        <div className="fl-waiting-words">
          <div className="fl-waiting-title">
            {w.item.url ? (
              <a className="fl-waiting-ref" href={w.item.url} target="_blank" rel="noreferrer" title={"Open " + w.item.reference}>
                {w.item.reference}
              </a>
            ) : (
              <span className="fl-waiting-ref">{w.item.reference}</span>
            )}
            <span className="fl-waiting-headline">{w.item.title}</span>
          </div>
          <div className="fl-waiting-meta muted">
            {where && <span className="fl-waiting-where">→ {where}</span>}
            {meta && <span>{meta}</span>}
            <span className={"fl-auto" + (w.auto ? " on" : "")}>{w.auto ? "starts by itself" : "starts when you say"}</span>
          </div>
        </div>
        <button type="button" className="btn-primary fl-btn fl-fly" disabled={busy} title={"Run on " + w.item.reference} onClick={() => void fly()}>
          {busy ? "Starting…" : "Run"}
        </button>
      </div>
      {plans.length > 0 && (
        <div className="fl-waiting-flyway">
          <span className="fl-fld-label">Route</span>
          <ChipRow className="fl-plan-pick" label="Route" value={plan} options={planChoices(plans, kind, plan)} onChange={setPicked} />
        </div>
      )}
      {error && (
        <p className="error fl-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export const KIND_ORDER: readonly WaitingKind[] = ["tickets", "prs", "issues"];

/** One flight perched on you (or halted): its headline (a press flies the
 * sky to its line), where it stopped, and the perch block — the checklist
 * with Done / Skip, or Retry / Skip. */
function PerchRow({ flight, kinds, onFocus }: { flight: Flight; kinds?: readonly StepKindDef[] | null; onFocus(title: string): void }) {
  const step = flight.steps[flight.cursor];
  const halted = flight.state === "halted";
  // The subject's headline, else the session's shown name (never its raw title).
  const head = flight.subject?.title || flight.subject?.id || !flight.session ? flightHeadline(flight) : windowName(flight.session);
  return (
    <div className={"sky-perch" + (halted ? " is-halted" : "")} data-flight={flight.id}>
      <button type="button" className="sky-perch-name" title={flight.session ? "Show " + windowName(flight.session) + "'s route" : undefined} onClick={() => onFocus(flight.session)}>
        {head}
      </button>
      <div className="sky-perch-step muted">
        {step ? (stepActor(step, kinds) === "you" ? perchLabel(step.title) : step.title) : ""}
        {halted && flight.reason ? " — " + haltedWhy(step?.title, flight.reason) : ""}
      </div>
      <YouStepActions flight={flight} />
    </div>
  );
}

/** Any other row of the one Needs-you list (lib/needsYou): a session asking
 * you something (its question and the answer strip — the bell's own), an
 * approval or escalation (the bell's waiting row), a plan waiting for Go, or
 * a session that is broken, failing its checks or ready for its PR. A press
 * on its name flies the sky to its route. */
function NeedsLine({ row, onFocus }: { row: NeedsYouRow; onFocus(title: string): void }) {
  const { data: instances } = useInstances();
  const inst = row.title ? instances?.find((i) => i.title === row.title) : undefined;
  if (row.waiting) {
    return (
      <div className="sky-perch sky-needs-wait" data-needs={row.key}>
        <OutboxWaitingRow w={row.waiting} row={inst} shown={windowName} group={row.waiting.run?.name || ""} />
      </div>
    );
  }
  const a = row.attn;
  const what = row.plan ? "Plan ready — review & Go" : a?.reason || "";
  return (
    <div className={"sky-perch sky-needs-" + row.kind} data-needs={row.key}>
      {row.title ? (
        <button type="button" className="sky-perch-name" title={"Show " + windowName(row.title) + "'s route"} onClick={() => onFocus(row.title)}>
          {windowName(row.title)}
        </button>
      ) : null}
      {what && row.kind !== "asking" && <div className="sky-perch-step muted">{what}</div>}
      {row.kind === "asking" && a ? (
        <AnswerStrip
          title={row.title}
          activity={a.question !== undefined ? "idle" : "clarify"}
          question={a.question}
          variant="bell"
          onOpen={() => onFocus(row.title)}
          onReply={() => openAsk(row.title)}
        />
      ) : row.plan ? (
        <button type="button" className="btn-primary fl-btn" onClick={() => useUi.getState().openMap(row.title, { section: "plan" })}>
          Review the plan
        </button>
      ) : null}
    </div>
  );
}

export interface SkyDrawerProps {
  open: boolean;
  onToggle(): void;
  /** Fly the sky to a session's line. */
  onFocus(title: string): void;
}

export function SkyDrawer({ open, onToggle, onFocus }: SkyDrawerProps) {
  // Its links: behaviors fly the sky (openBehaviors), sources open Intake.
  const openDialogFor = useUi((s) => s.openDialogFor);
  const flightsQ = useFlights();
  const plans = useFlightPlans().data || [];
  const kinds = useFlightKinds().data;
  const settings = useSettingsDoc(true).data as Record<string, unknown> | undefined;
  const { waiting, refresh } = useWaiting(true);
  const [landedOpen, setLandedOpen] = useState(false);

  const flights: Flight[] = flightsQ.data || [];
  // THE one Needs-you list (lib/needsYou) — the bell's rows, in the bell's
  // order, so this strip's count and the bell's badge are one number.
  const needs = useNeedsYou();
  const landed = flights.filter((f) => !isLive(f)).sort((a, b) => (b.finished || b.updated) - (a.finished || a.updated)).slice(0, 20);
  const count = needs.length;

  return (
    <aside className={"sky-drawer" + (open ? " open" : "")} aria-label="Needs you" data-tour="flights-drawer">
      <button
        type="button"
        className={"sky-drawer-tab" + (count > 0 ? " has-perch" : "")}
        aria-expanded={open}
        title={open ? "Fold the drawer away" : count > 0 ? count + " need you — open the drawer" : "What needs you"}
        onClick={onToggle}
      >
        <span className="sky-drawer-tab-word">Needs you</span>
        {count > 0 && <span className="fl-count fl-count-you">{count}</span>}
      </button>
      {open && (
        <div className="sky-drawer-body">
          {flightsQ.data === null && (
            <p className="fl-note muted" role="status">
              This server can't run routes yet — update MindFlock to run them. Waiting items and behaviors still work.
            </p>
          )}
          <section className="fl-group fl-group-needs" id="fl-group-needs" aria-labelledby="fl-group-needs-h">
            <h3 id="fl-group-needs-h" className="fl-group-head">
              Needs you
              {count > 0 && <span className="fl-count">{count}</span>}
            </h3>
            {count === 0 ? (
              <p className="fl-empty muted">Nothing needs you.</p>
            ) : (
              <>
                {needs.map((r) =>
                  r.flight ? (
                    <PerchRow key={r.key} flight={r.flight} kinds={kinds} onFocus={onFocus} />
                  ) : r.behaviors ? (
                    <ChecklistRow key={r.key} plan={r.behaviors} onOpen={(planId) => openBehaviors({ planId })} />
                  ) : (
                    <NeedsLine key={r.key} row={r} onFocus={onFocus} />
                  )
                )}
              </>
            )}
          </section>
          <details className="fl-group fl-group-waiting" id="fl-group-waiting">
            <summary className="fl-group-head">
              Waiting to start
              {waiting.length > 0 && <span className="fl-count">{waiting.length}</span>}
            </summary>
            {waiting.length === 0 ? (
              <p className="fl-empty muted">
                Nothing is waiting to start.{" "}
                <button type="button" className="linklike" onClick={() => openDialogFor("intake")}>
                  Intake ›
                </button>{" "}
                <button type="button" className="linklike" onClick={refresh}>
                  Refresh
                </button>
              </p>
            ) : (
              KIND_ORDER.map((k) => {
                const list = waiting.filter((w) => w.item.kind === k);
                if (!list.length) return null;
                return (
                  <div className={"fl-waiting-kind fl-waiting-kind-" + k} key={k}>
                    <h4 className="fl-waiting-head">
                      {WAITING_KIND_HEAD[k]}
                      <span className="fl-count muted">{list.length}</span>
                    </h4>
                    {list.map((w) => (
                      <WaitingRow key={w.item.key} w={w} plans={plans} settings={settings} />
                    ))}
                  </div>
                );
              })
            )}
          </details>
          {landed.length > 0 && (
            <details className="fl-group fl-group-landed" open={landedOpen} onToggle={(e) => setLandedOpen((e.target as HTMLDetailsElement).open)}>
              <summary className="fl-group-head">
                Finished<span className="fl-count muted">{landed.length}</span>
              </summary>
              {landedOpen && landed.map((f) => <FlightCard key={f.id} flight={f} kinds={kinds} />)}
            </details>
          )}
        </div>
      )}
    </aside>
  );
}
