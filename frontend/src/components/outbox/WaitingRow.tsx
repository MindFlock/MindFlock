/** The bell's waiting rows — what MindFlock stopped for, and the one click
 * that moves it on (SPEC §7.C.3), listed in the bell's "Needs attention". They
 * come from `GET /api/outbox` (the folder keeps the route's name).
 *
 * There are only two kinds of "you" here: ANSWER a prompt an agent is stuck
 * on (the shared AnswerStrip), or APPROVE a ship you asked to see first — and
 * that row shows the exact commit message and PR title BEFORE anything leaves
 * the machine. Escalations (a stuck group line, a spent budget, a lead's plan
 * or PR) are the second kind, each with its own buttons. Every action posts at
 * once and reports failure in the error card — no `window.prompt` / `confirm`
 * (Electron has neither): the commit message is edited in an inline field. */

import { useState } from "react";
import type { Instance, OutboxWaiting } from "../../api/types";
import { instApi } from "../../api/client";
import { refreshInstances, useFlights, useInstances } from "../../state/queries";
import { refreshRuns } from "../../state/runs";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { effectiveActivity } from "../../lib/stage";
import { haltedWhy } from "../../lib/sessionState";
import { escalationText, laneOf } from "../../lib/agentMessages";
import { errMsg } from "../../lib/format";
import { errorPop } from "../../lib/errorPop";
import { toast } from "../../lib/toast";
import { AnswerStrip } from "../AnswerStrip";
import {
  approvalKey,
  messageHead,
  shipVerb,
  statText,
  thenText,
  waitingActions,
  waitingChip,
  LEAD_KINDS,
  type WaitAction,
} from "./outbox";
import { cancelRun, raiseBudget, runAction as act, runPath, suggestedBudget, taskPath } from "../../lib/runsApi";
import { releaseChoices } from "../../lib/splitRun";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { controlFlight, flightAction, flightPath, stepDone, stepRetry, stepSkip } from "../../lib/flightsApi";
import { perchedOnYou, type Flight } from "../../lib/flights";
import { sessionGone, skipCarriesOn } from "../flights/FlightStrip";
import { askOffers, findingsOf, flightAsk, owedGroups } from "../../lib/stopAsks";
import { OwedChecks, ReviewFindings } from "../flights/PerchAsks";
import type { RunInfo } from "../../lib/runs";
import { FLIGHT_PAUSED, pausedAtLine, type FlightWaiting } from "../../lib/needsYou";

/** Take the user to a session's pane (optionally a tab of it), closing any
 * open dialog and the bell ("mf-close-bell") — the pane is where the thing
 * being pointed at lives. */
export function openSession(title: string, tab?: string) {
  selectSession(title);
  if (tab) useUi.getState().setLastTab(title, tab);
  useUi.getState().closeDialog();
  document.dispatchEvent(new CustomEvent("mf-close-bell"));
}

/** The left half every row shares: the session (mono, accent — click opens
 * it) and what it is about. */
export function RowMain({
  title,
  text,
  shown,
  row,
  closed = false,
}: {
  title: string;
  text?: string;
  shown: (t: string) => string;
  row?: Instance;
  /** The session is closed: its name, said so, and no link to it. */
  closed?: boolean;
}) {
  const about = text || row?.last_prompt || "";
  return (
    <div className="pr-open-main">
      {closed ? (
        <span className="pr-open-ref ob-ref" title={shown(title) + " is closed"}>
          {shown(title)}
        </span>
      ) : row ? (
        <button type="button" className="pr-open-ref ob-ref" title={"Open " + title} onClick={() => openSession(title)}>
          {shown(title)}
        </button>
      ) : (
        <span className="pr-open-ref ob-ref">{shown(title)}</span>
      )}
      {about && <span className="pr-open-title">{about}</span>}
    </div>
  );
}

/** What a Waiting row's button posts (Open is the caller's: it navigates). */
function doWaitAction(key: WaitAction["key"], w: OutboxWaiting, runId: string, taskId: string): Promise<boolean> {
  switch (key) {
    case "retry":
      return act("Retry", taskPath(runId, taskId, "retry"), { fresh: false }, "Retrying " + w.title);
    case "retry_fresh":
      return act("Retry fresh", taskPath(runId, taskId, "retry"), { fresh: true }, "Retrying " + w.title + " on a fresh branch");
    case "skip":
      return act("Skip", taskPath(runId, taskId, "skip"), {}, "Skipped " + w.title);
    case "approve":
      return act("Start the workers", runPath(runId, "/plan/approve"), {}, "Starting the workers — each fenced to its paths");
    case "release": {
      // The same words as the button: a push-only lane or a local-folder
      // origin pushes the branch and opens no PR.
      const pushOnly = !!w.preview?.local_origin || w.preview?.lane === "push";
      const what = releaseChoices(w.preview?.lane, w.preview?.local_origin).find((c) => !c.merge)?.label || "Open PR";
      return act(
        what,
        runPath(runId, "/release"),
        { merge_when_green: false },
        pushOnly ? "Pushing the team run's branch" : "Opening the team run's PR"
      );
    }
    case "release_merge":
      return act(
        "Open PR",
        runPath(runId, "/release"),
        { merge_when_green: true },
        "Opening the PR — it merges once checks pass"
      );
    case "retry_check":
      return act("Run the check", runPath(runId, "/check"), {}, "Running the check again");
    case "cancel_group":
      return cancelRun(runId, w.run?.name || "");
    default:
      return Promise.resolve(false);
  }
}

export function WaitingRow({
  w,
  row,
  shown,
  group,
  runInfo,
}: {
  w: OutboxWaiting;
  row?: Instance;
  shown: (t: string) => string;
  group: string;
  /** The group this row is about, for a budget item's numbers. */
  runInfo?: RunInfo;
}) {
  const [busy, setBusy] = useState(false);
  const chip = waitingChip(w);
  const runId = w.run?.id || row?.run?.id || "";
  const taskId = w.run?.task || row?.run?.task || "";
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  const escalation = w.kind !== "prompt" && w.kind !== "approve";
  if (w.kind === "budget") return <BudgetRow w={w} runInfo={runInfo} />;
  if (w.kind === "flight_you") return <FlightYouRow w={w} row={row} shown={shown} />;
  if (w.kind === "flight_halted") return <FlightHaltedRow w={w} row={row} shown={shown} />;
  if (w.kind === FLIGHT_PAUSED) return <FlightPausedRow w={w} row={row} shown={shown} />;
  return (
    <div className={"pr-open-item ob-item ob-" + (escalation ? "escalation" : w.kind)} data-outbox-row={w.title}>
      <RowMain title={w.title} text={w.text} shown={shown} row={row} />
      <div className="pr-open-meta">
        <span className={"pr-open-chip" + (chip.cls ? " " + chip.cls : "")}>
          {escalation ? escalationText(chip.text) : chip.text}
        </span>
        {w.kind === "approve" && statText(w.preview) && <span>{statText(w.preview)}</span>}
        {group && <span>{group}</span>}
      </div>
      <div className="ik-item-start">
        {w.kind === "approve" ? (
          <ApproveButtons w={w} busy={busy} setBusy={setBusy} hasRow={!!row} />
        ) : (
          // ONE horizontal row, the primary first (Retry · Open ↗ · Skip).
          <div className="ob-acts">
            {waitingActions(w, { row: !!row, run: !!runId, task: !!taskId }).map((a) => (
              <button
                key={a.key}
                type="button"
                className={a.primary ? "btn-primary pr-review-btn" : "test-btn"}
                disabled={busy && a.key !== "open"}
                title={a.title}
                onClick={() => {
                  if (a.key === "open") {
                    // A group-level row is about the lead's Thread; a conflict
                    // row names the piece, whose lead is its parent.
                    const lead = w.kind === "conflict" ? row?.parent || w.title : w.title;
                    openSession(LEAD_KINDS.has(w.kind) ? lead : w.title, LEAD_KINDS.has(w.kind) ? "thread" : undefined);
                    return;
                  }
                  void run(() => doWaitAction(a.key, w, runId, taskId));
                }}
              >
                {a.label}
              </button>
            ))}
          </div>
        )}
      </div>
      {w.kind === "prompt" && row && (
        <div className="ik-item-drawer">
          <AnswerStrip
            title={w.title}
            activity={effectiveActivity(row)}
            variant="thread"
            onOpen={() => openSession(w.title)}
          />
        </div>
      )}
      {w.kind === "approve" && (
        <div className="ik-item-drawer">
          <ApprovePreview w={w} row={row} />
        </div>
      )}
      {w.kind === "plan" && (w.preview?.pieces?.length || 0) > 0 && (
        <div className="ik-item-drawer">
          <div className="ob-card ob-plan">
            {w.preview!.pieces!.map((p, i) => (
              <div key={i} className="ob-plan-piece">
                <b>{p.title}</b>
                <span className="ob-only">
                  only here: <code>{(p.paths || []).join(", ") || "—"}</code>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
      {w.kind === "release" && w.preview && (
        <div className="ik-item-drawer">
          <div className="ob-card">
            <div className="ob-kv">
              {w.preview.pr_title && (
                <>
                  <span className="ob-k">PR title</span>
                  <span className="ob-pr-title">{w.preview.pr_title}</span>
                </>
              )}
              <span className="ob-k">Into</span>
              <span className="ob-mono">
                {(w.preview.base || "base") + " ← " + (w.preview.branch || w.title)}
                {statText(w.preview) ? " · " + statText(w.preview) : ""}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** A group that spent its budget: it is paused (agents keep working, nothing
 * new starts or ships) until you raise it — `resume {budget_usd}`, one call —
 * or stop it (`cancel`). The amount is typed inline (Electron has no prompt). */
function BudgetRow({ w, runInfo }: { w: OutboxWaiting; runInfo?: RunInfo }) {
  const runId = w.run?.id || "";
  const name = w.run?.name || runInfo?.name || "The team run";
  const budget = Number(runInfo?.budget_usd) || 0;
  const spent = Number(runInfo?.cost_usd) || 0;
  const [usd, setUsd] = useState(() => String(suggestedBudget(budget, spent)));
  const [busy, setBusy] = useState(false);
  const amount = Number(usd);
  const valid = Number.isFinite(amount) && amount > spent;
  const go = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="pr-open-item ob-item ob-escalation ob-budget" data-outbox-row={"budget:" + runId}>
      <div className="pr-open-main">
        <span className="pr-open-ref ob-ref">{name}</span>
        <span className="pr-open-title">
          {w.reason || "the team run's budget is used up"}
          {budget ? " — spent $" + spent.toFixed(2) + " of $" + budget.toFixed(2) : ""}
        </span>
      </div>
      <div className="pr-open-meta">
        <span className="pr-open-chip bad">paused — budget</span>
      </div>
      <div className="ik-item-start">
        {(w.actions || []).includes("raise_budget") && runId && (
          <>
            <span className="ob-usd">
              $
              <input
                type="number"
                min={0}
                step="any"
                aria-label="New budget in dollars"
                value={usd}
                disabled={busy}
                onChange={(e) => setUsd(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && valid) void go(() => raiseBudget(runId, amount, name));
                }}
              />
            </span>
            <button
              type="button"
              className="btn-primary pr-review-btn"
              disabled={busy || !valid}
              title={valid ? "Raise the budget and resume the team run" : "More than it has spent ($" + spent.toFixed(2) + ")"}
              onClick={() => go(() => raiseBudget(runId, amount, name))}
            >
              Raise to ${valid ? amount : "…"}
            </button>
          </>
        )}
        {(w.actions || []).includes("stop") && runId && (
          <button
            type="button"
            className="test-btn"
            disabled={busy}
            title="Cancel the team run — nothing new starts or ships; sessions and branches stay"
            onClick={() => go(() => cancelRun(runId, name))}
          >
            Stop
          </button>
        )}
      </div>
    </div>
  );
}

/** The message a "Commit" will use: what the server previewed, or what you
 * typed over it in the preview card — kept per APPROVAL (approvalKey), so an
 * edit never outlives its card: a later approval of the same session starts
 * from the server's preview, not from text typed for different work. */
const editedMessage = new Map<string, string>();

function ApproveButtons({
  w,
  busy,
  setBusy,
  hasRow,
}: {
  w: OutboxWaiting;
  busy: boolean;
  setBusy(b: boolean): void;
  hasRow: boolean;
}) {
  const verb = shipVerb(w.step);
  return (
    <>
      {hasRow && (
        <button type="button" className="test-btn" title="Open its Diff tab" onClick={() => openSession(w.title, "diff")}>
          Diff
        </button>
      )}
      <button
        type="button"
        className="btn-primary pr-review-btn ob-ship"
        disabled={busy}
        title={verb + " now, with the message and title shown below"}
        onClick={async () => {
          setBusy(true);
          const msg = editedMessage.get(approvalKey(w));
          try {
            // ship-now: take what's there through the lane, past the ask-first
            // stop. An edited message rides along; the server's own preview is
            // what goes otherwise.
            await instApi(w.title, "/ship-now", { json: msg ? { commit_message: msg } : {} });
            toast(verb + ": " + w.title);
            editedMessage.delete(approvalKey(w));
          } catch (err) {
            errorPop(verb + " failed — " + w.title, errMsg(err));
          } finally {
            setBusy(false);
            void refreshRuns();
            void refreshInstances();
          }
        }}
      >
        {verb}
      </button>
    </>
  );
}

function ApprovePreview({ w, row }: { w: OutboxWaiting; row?: Instance }) {
  const p = w.preview || null;
  const [editing, setEditing] = useState(false);
  const editKey = approvalKey(w);
  const [msg, setMsg] = useState(() => editedMessage.get(editKey) ?? p?.commit_message ?? "");
  const lane = w.lane || (row ? laneOf(row)?.target : "") || "";
  // The message can be set only before the commit: a lane held before its
  // push has committed already (its message is history).
  const canEdit = (w.actions || []).includes("edit_message") && (w.step || "commit") === "commit";
  return (
    <div className="ob-card">
      <div className="ob-kv">
        {(p?.commit_message || msg || canEdit) && (
          <>
            <span>Message</span>
            {editing ? (
              <textarea
                className="ob-msg-edit"
                value={msg}
                autoFocus
                rows={Math.min(8, Math.max(2, msg.split("\n").length))}
                spellCheck={false}
                aria-label="Commit message"
                title="Enter for a new line; Ctrl+Enter (or click away) to keep it; Escape to undo"
                onChange={(e) => setMsg(e.target.value)}
                onBlur={() => {
                  setEditing(false);
                  if (msg.trim() && msg !== p?.commit_message) editedMessage.set(editKey, msg.trim());
                  else editedMessage.delete(editKey);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) (e.target as HTMLTextAreaElement).blur();
                  if (e.key === "Escape") {
                    e.stopPropagation();
                    setMsg(p?.commit_message || "");
                    editedMessage.delete(editKey);
                    setEditing(false);
                  }
                }}
              />
            ) : (
              <span className="ob-mono">
                {messageHead(msg) || <span className="muted">written from the diff when it commits</span>}
                {canEdit && (
                  <button type="button" className="linklike ob-edit" onClick={() => setEditing(true)}>
                    edit
                  </button>
                )}
              </span>
            )}
          </>
        )}
        {p?.pr_title && (
          <>
            <span>PR title</span>
            <span className="ob-pr-title">{p.pr_title}</span>
          </>
        )}
        <span>Then</span>
        <span>{thenText(w.step, lane)}</span>
      </div>
    </div>
  );
}

/** A flight perched on you (a `you` stop, docs/flights.md): Done / Skip right here,
 * from the session row's `flight` (its id and the step's), and Open ↗ to go
 * look first. */
/** The route and its current stop behind a flight row: the row's own (a
 * route with no session has no session row), else the session row's. */
function flightOf(
  w: OutboxWaiting,
  row?: Instance,
  full?: Flight
): { id: string; step: { id: string; kind: string; title: string } } | null {
  const own = (w as FlightWaiting).flight;
  const fl = row?.flight;
  if (fl && fl.id && (!own || own.id === fl.id)) return { id: fl.id, step: fl.step };
  if (own && own.id && own.step_id) {
    // No session row: the stop's kind from the full flight — a Verify stop
    // must not offer the Done the server refuses.
    const at = full && full.id === own.id ? full.steps.find((x) => x.id === own.step_id) : undefined;
    return { id: own.id, step: { id: own.step_id, kind: at?.kind || "", title: own.step_title } };
  }
  return null;
}

/** The route behind a flight row as the full flight (when the flights are
 * read), and whether its session is closed (FlightStrip `sessionGone`: not
 * among the rows; unknown while they load = not closed). */
function useRoute(w: OutboxWaiting, row?: Instance) {
  const flights = useFlights().data;
  const instances = useInstances().data;
  const id = (w as FlightWaiting).flight?.id || row?.flight?.id || "";
  const full = id ? flights?.find((f) => f.id === id) : undefined;
  const gone = !row && !!full && sessionGone(full, instances);
  return { full, gone, fl: flightOf(w, row, full) };
}

/** Skip on a route paused at a halt point: the server keeps it paused after
 * a skip, so Skip carries on too — where the Sky's strip does
 * (FlightStrip `skipCarriesOn`): not into a second halt point, and not past
 * the last stop ("Carry on failed — already finished"). Unknown route: no
 * Carry on. */
export async function skipPausedStop(fl: { id: string; step: { id: string } }, full?: Flight): Promise<unknown> {
  return (await stepSkip(fl.id, fl.step.id)) && !!full && skipCarriesOn(full) && controlFlight(fl.id, "resume");
}

/** Its session is closed: Stop the route ends it (the Sky's words,
 * FlightStrip `YouStepActions gone`). */
function StopRouteButton({ id, busy, run }: { id: string; busy: boolean; run: (fn: () => Promise<unknown>) => Promise<void> }) {
  return (
    <button
      type="button"
      className="test-btn"
      disabled={busy}
      title="Its session is closed — end this route"
      onClick={() => void run(() => flightAction("Stop the route", flightPath(id, "/cancel"), {}, "Route stopped"))}
    >
      Stop the route
    </button>
  );
}

/** The left half of a route's row: its session — or, with the session
 * closed, that session's name said as closed — and the route. */
function RouteMain({ w, row, shown, full, gone }: { w: OutboxWaiting; row?: Instance; shown: (t: string) => string; full?: Flight; gone: boolean }) {
  if (gone && full) return <RowMain title={full.session} text={w.text || full.plan_name} shown={shown} closed />;
  return <RowMain title={w.title || w.text || ""} text={w.title ? w.text : ""} shown={w.title ? shown : (t) => t} row={row} />;
}

function FlightYouRow({ w, row, shown }: { w: OutboxWaiting; row?: Instance; shown: (t: string) => string }) {
  const [busy, setBusy] = useState(false);
  // A Review out of fix rounds, the perch asking for the checks owed at the
  // end (lib/stopAsks): the full flight says which, and their words lead.
  const { full, gone, fl } = useRoute(w, row);
  const ask = full && full.steps[full.cursor]?.id === fl?.step.id ? flightAsk(full) : "";
  const offers = askOffers(ask);
  const rec = full && fl ? full.step_states?.[fl.step.id] : undefined;
  const owed = ask === "owed" ? owedGroups(rec?.detail?.owed) : [];
  const findings = ask === "unresolved" ? findingsOf(rec?.detail) : "";
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="pr-open-item ob-item ob-flight_you" data-outbox-row={w.title}>
      <RouteMain w={w} row={row} shown={shown} full={full} gone={gone} />
      <div className="pr-open-meta">
        <span className="pr-open-chip warn" title={w.reason || undefined}>
          {perchedOnYou(fl?.step?.title || "")}
        </span>
      </div>
      <div className="ik-item-start">
        <div className="ob-acts">
          {fl && offers ? (
            <>
              {offers.map((o) => (
                <button
                  key={o.key}
                  type="button"
                  className={o.primary ? "btn-primary pr-review-btn" : "test-btn"}
                  disabled={busy}
                  title={o.title}
                  onClick={() => void run(() => (o.key === "retry" ? stepRetry(fl.id, fl.step.id) : stepDone(fl.id, fl.step.id)))}
                >
                  {o.label}
                </button>
              ))}
              {ask === "owed" && (
                <button
                  type="button"
                  className="test-btn"
                  disabled={busy}
                  title={"Skip “" + fl.step.title + "” — the checks stay unconfirmed and the route goes on"}
                  onClick={() => void run(() => stepSkip(fl.id, fl.step.id))}
                >
                  Skip
                </button>
              )}
            </>
          ) : fl && (
            <>
              {fl.step.kind === "verify" ? (
                // A Verify stop moves on when its Verify is confirmed, not on
                // Done (the server refuses it): the session's Verify opens.
                <button
                  type="button"
                  className="btn-primary pr-review-btn"
                  title={"Open the Verify of “" + fl.step.title + "” — the route goes on once you confirm it"}
                  onClick={() => openBehaviors({ title: w.title })}
                >
                  Verify
                </button>
              ) : (
                <button
                  type="button"
                  className="btn-primary pr-review-btn"
                  disabled={busy}
                  title={"Mark “" + fl.step.title + "” done — the route goes on"}
                  onClick={() => void run(() => stepDone(fl.id, fl.step.id))}
                >
                  Done
                </button>
              )}
              <button
                type="button"
                className="test-btn"
                disabled={busy}
                title={"Skip “" + fl.step.title + "” — the route goes on without it"}
                onClick={() => void run(() => stepSkip(fl.id, fl.step.id))}
              >
                Skip
              </button>
            </>
          )}
          {fl && gone && <StopRouteButton id={fl.id} busy={busy} run={run} />}
          {row && (
            <button type="button" className="test-btn" onClick={() => openSession(w.title)}>
              Open ↗
            </button>
          )}
        </div>
      </div>
      {(findings || owed.length > 0) && (
        <div className="ik-item-drawer">
          <ReviewFindings findings={findings} />
          <OwedChecks groups={owed} session={w.title || full?.session} />
        </div>
      )}
    </div>
  );
}

/** A route halted at a stop (docs/vocabulary.md "Halt"): why, and Retry /
 * Skip right here — the same two the stop itself and the Sky's Needs you
 * panel offer — with Open ↗ to go look first. */
function FlightHaltedRow({ w, row, shown }: { w: OutboxWaiting; row?: Instance; shown: (t: string) => string }) {
  const [busy, setBusy] = useState(false);
  const { full, gone, fl } = useRoute(w, row);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  const at = fl?.step?.title || "";
  const chip = "Halted" + (at ? " at " + at : "");
  // The chip names the stop; the line says what failed, without it again
  // (and not at all when the reason is only the chip's own words).
  const why = haltedWhy(at, w.reason);
  return (
    <div className="pr-open-item ob-item ob-flight_halted" data-outbox-row={w.title || w.key}>
      <RouteMain w={w} row={row} shown={shown} full={full} gone={gone} />
      <div className="pr-open-meta">
        <span className="pr-open-chip bad" title={w.reason || undefined}>
          {chip}
        </span>
      </div>
      {!!why && why !== chip && <div className="ob-halt-reason">{why}</div>}
      <div className="ik-item-start">
        <div className="ob-acts">
          {fl && (
            <>
              <button
                type="button"
                className="btn-primary pr-review-btn"
                disabled={busy}
                title={"Run “" + (at || "this stop") + "” again"}
                onClick={() => void run(() => stepRetry(fl.id, fl.step.id))}
              >
                Retry
              </button>
              <button
                type="button"
                className="test-btn"
                disabled={busy}
                title={"Skip “" + (at || "this stop") + "” — the route goes on without it"}
                onClick={() => void run(() => stepSkip(fl.id, fl.step.id))}
              >
                Skip
              </button>
            </>
          )}
          {fl && gone && <StopRouteButton id={fl.id} busy={busy} run={run} />}
          {row && (
            <button type="button" className="test-btn" onClick={() => openSession(w.title)}>
              Open ↗
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** A route paused at a halt point a person set (docs/vocabulary.md "Halt
 * point"): a calm chip ("Paused at <stop>"), Carry on — the stop runs — or
 * Skip — the route goes on without it — right here, with Open ↗ to go look
 * first. */
function FlightPausedRow({ w, row, shown }: { w: OutboxWaiting; row?: Instance; shown: (t: string) => string }) {
  const [busy, setBusy] = useState(false);
  const { full, gone, fl } = useRoute(w, row);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  const at = fl?.step?.title || "";
  const chip = pausedAtLine(at).replace(/ — Carry on$/, "");
  return (
    <div className="pr-open-item ob-item ob-flight_paused" data-outbox-row={w.title || w.key}>
      <RouteMain w={w} row={row} shown={shown} full={full} gone={gone} />
      <div className="pr-open-meta">
        <span className="pr-open-chip" title={w.reason || undefined}>
          {chip}
        </span>
      </div>
      <div className="ik-item-start">
        <div className="ob-acts">
          {fl && (
            <>
              <button
                type="button"
                className="btn-primary pr-review-btn"
                disabled={busy}
                title={"Carry on — “" + (at || "this stop") + "” runs and the route goes on"}
                onClick={() => void run(() => controlFlight(fl.id, "resume"))}
              >
                Carry on
              </button>
              <button
                type="button"
                className="test-btn"
                disabled={busy}
                title={"Skip “" + (at || "this stop") + "” — the route goes on without it"}
                // A skip alone leaves a paused route paused at the next stop:
                // carry on after it where the Sky does (`skipPausedStop`).
                onClick={() => void run(() => skipPausedStop(fl, full))}
              >
                Skip
              </button>
            </>
          )}
          {fl && gone && <StopRouteButton id={fl.id} busy={busy} run={run} />}
          {row && (
            <button type="button" className="test-btn" onClick={() => openSession(w.title)}>
              Open ↗
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
