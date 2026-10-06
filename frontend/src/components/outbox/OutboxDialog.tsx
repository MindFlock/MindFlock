/** The Outbox — what's shipping, and what's waiting on you (SPEC §7.C.3).
 *
 * The middle of the pipeline: Intake (in) → Outbox (out) → Verify (checked),
 * and built from the same kit, so it reads as the same anatomy a third time —
 * a tab strip with live counts, then collapsible groups of work rows.
 *
 * There are only two kinds of "you" here, and everything under Waiting on you
 * is one of them: ANSWER a prompt an agent is stuck on (the shared AnswerStrip),
 * or APPROVE a ship you asked to see first — and that row shows the exact commit
 * message and PR title BEFORE anything leaves the machine. Everything else is
 * "Shipping now": MindFlock is doing it, read-only. No surprise pushes.
 *
 * Tabs filter one response (outbox.ts says why); a tab per group with live rows,
 * plus "On their own" once any group exists. Every action posts at once and
 * reports failure in the error card — no `window.prompt` / `confirm` (Electron
 * has neither): the commit message is edited in an inline field. */

import { useEffect, useMemo, useState } from "react";
import type { Instance, OutboxQueued, OutboxShipped, OutboxShipping, OutboxSummary, OutboxWaiting } from "../../api/types";
import { instApi } from "../../api/client";
import { refreshInstances, useInstances } from "../../state/queries";
import { refreshRuns, useOutbox, useRuns } from "../../state/runs";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { effectiveActivity } from "../../lib/stage";
import { escalationText, laneOf } from "../../lib/agentMessages";
import { copyText } from "../../lib/clipboard";
import { errMsg } from "../../lib/format";
import { errorPop } from "../../lib/errorPop";
import { toast } from "../../lib/toast";
import { AnswerStrip } from "../AnswerStrip";
import { useToggleSet, WorkGroup } from "../intake/kit";
import {
  approvalKey,
  messageHead,
  outboxTabs,
  shippedChip,
  shipVerb,
  statText,
  thenText,
  viewCount,
  viewFor,
  waitingActions,
  waitingChip,
  LEAD_KINDS,
  type OutboxTabKey,
  type WaitAction,
} from "./outbox";

import { cancelRun, raiseBudget, runAction as act, runPath, suggestedBudget, taskPath } from "../../lib/runsApi";
import type { RunInfo } from "../../lib/runs";

type RowOf = (title: string) => Instance | undefined;

/** Take the user to a session's pane (optionally a tab of it), closing the
 * Outbox — the pane is where the thing being pointed at lives. */
function openSession(title: string, tab?: string) {
  selectSession(title);
  if (tab) useUi.getState().setLastTab(title, tab);
  useUi.getState().closeDialog();
}

export function OutboxDialog() {
  const open = useUi((s) => s.openDialog === "outbox");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const aliases = useUi((s) => s.aliases);
  const [tab, setTab] = useState<OutboxTabKey>("all");
  const { data, error, isFetching } = useOutbox();
  const { data: instances = [] } = useInstances();
  const { data: runs } = useRuns();
  // Sections default OPEN (the rows are why you came); the set holds the ones
  // you folded, per device.
  const folds = useToggleSet("mf_outbox_folded", true);

  // A bell row or a group header opens the Outbox on its group.
  useEffect(() => {
    if (!open) return;
    setTab(target || "all");
    void refreshRuns();
  }, [open, target]);

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

  const byTitle = useMemo(() => new Map(instances.map((i) => [i.title, i])), [instances]);
  const rowOf: RowOf = (t) => byTitle.get(t);
  const runName = (id: string) => runs?.find((r) => r.id === id)?.name || "";

  if (!open) return null;

  const tabs = outboxTabs(data, rowOf, runName);
  // A group asked for by name stays reachable even with nothing left in it —
  // that is where its Summary card is.
  if (tab !== "all" && !tabs.some((t) => t.key === tab))
    tabs.push({ key: tab, label: tab === "own" ? "On their own" : runName(tab) || "Group", count: 0 });
  const view = viewFor(data, tab, rowOf);
  const total = viewCount(view);
  const groupLabel = (it: { run?: { id?: string; name?: string } | null; title?: string }) => {
    if (tab !== "all") return "";
    const id = it.run?.id || (it.title ? rowOf(it.title)?.run?.id : "") || "";
    return id ? it.run?.name || rowOf(it.title || "")?.run?.name || runName(id) : "";
  };
  const shown = (t: string) => aliases[t] || t;
  const section = (key: string, name: string, count: number, detail: string, body: React.ReactNode) =>
    count > 0 ? (
      <WorkGroup
        key={key}
        name={name}
        count={count}
        detail={detail}
        heading
        open={folds.isOpen(key)}
        onToggle={() => folds.toggle(key)}
      >
        <div className="pr-open-list">{body}</div>
      </WorkGroup>
    ) : null;

  return (
    <div
      id="outbox-dialog"
      className="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="outbox-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="outbox-panel">
        <div className="ws-head">
          <h2 id="outbox-title">Outbox</h2>
          <span className="ik-subtitle">What's shipping, and what's waiting on you</span>
          {isFetching && data ? <span className="ob-fetch">refreshing…</span> : null}
          <button type="button" id="outbox-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <nav id="outbox-tabs" aria-label="Outbox groups">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              className={"ik-tab" + (tab === t.key ? " active" : "")}
              data-outbox-tab={t.key}
              aria-current={tab === t.key ? "page" : undefined}
              onClick={() => setTab(t.key)}
            >
              {t.label}
              {t.count > 0 && <span className="ik-tab-count">{t.count}</span>}
            </button>
          ))}
        </nav>
        <div id="outbox-body">
          {data === null ? (
            <p className="repo-empty">
              This MindFlock server has no Outbox yet — update it, then sessions you start
              together (and any session with a lane) show up here.
            </p>
          ) : error && !data ? (
            <p className="repo-empty">Could not load the Outbox: {errMsg(error)}</p>
          ) : !data ? (
            <p className="repo-empty">Loading…</p>
          ) : (
            <div className="ik-groups ob-groups">
              {total === 0 && !view.summaries.length && (
                <p className="repo-empty">
                  Nothing is shipping and nothing is waiting on you. Give a session a lane — or
                  start several together from New — and it shows up here on its way out.
                </p>
              )}
              {/* A group's own tab (where its finish notification lands) is
                  topped by its summary; under All the summaries come last. */}
              {tab !== "all" && view.summaries.map((s) => <SummaryCard key={"sum:" + s.run} s={s} />)}
              {section(
                "waiting",
                "Waiting on you",
                view.waiting.length,
                "answer or approve — nothing else needs you",
                view.waiting.map((w) => (
                  <WaitingRow
                    key={"w:" + (w.key || w.title) + ":" + w.kind}
                    w={w}
                    row={w.title ? rowOf(w.title) : undefined}
                    shown={shown}
                    group={groupLabel(w)}
                    runInfo={w.run?.id ? runs?.find((r) => r.id === w.run!.id) : undefined}
                  />
                ))
              )}
              {section(
                "shipping",
                "Shipping now",
                view.shipping.length,
                "MindFlock is doing these — no action",
                view.shipping.map((s) => (
                  <ShippingRow key={"s:" + (s.key || s.title)} s={s} row={rowOf(s.title)} shown={shown} group={groupLabel(s)} />
                ))
              )}
              {section(
                "shipped",
                "Shipped today",
                view.shipped.length,
                "",
                view.shipped.map((s) => (
                  <ShippedRow key={"d:" + (s.key || s.title)} s={s} row={rowOf(s.title)} shown={shown} group={groupLabel(s)} />
                ))
              )}
              {section(
                "queued",
                "Queued",
                view.queued.length,
                "they start as slots free",
                view.queued.map((q) => (
                  <QueuedItem key={"q:" + q.run.id + ":" + q.run.task} q={q} group={tab === "all" ? q.run.name || runName(q.run.id) : ""} />
                ))
              )}
              {tab === "all" && view.summaries.map((s) => <SummaryCard key={"sum:" + s.run} s={s} />)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** The left half every row shares: the session (mono, accent — click opens
 * it) and what it is about. */
function RowMain({ title, text, shown, row }: { title: string; text?: string; shown: (t: string) => string; row?: Instance }) {
  const about = text || row?.last_prompt || "";
  return (
    <div className="pr-open-main">
      {row ? (
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
    case "release":
      return act("Open the PR", runPath(runId, "/release"), { merge_when_green: false }, "Releasing the group");
    case "release_merge":
      return act(
        "Open the PR",
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

function WaitingRow({
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
  const name = w.run?.name || runInfo?.name || "The group";
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
          {w.reason || "the group's budget is used up"}
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
              title={valid ? "Raise the budget and resume the group" : "More than it has spent ($" + spent.toFixed(2) + ")"}
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
            title="Cancel the group — nothing new starts or ships; sessions and branches stay"
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

function ShippingRow({
  s,
  row,
  shown,
  group,
}: {
  s: OutboxShipping;
  row?: Instance;
  shown: (t: string) => string;
  group: string;
}) {
  const reported = row?.last_report && String(row.last_report.status).toLowerCase() === "done";
  return (
    <div className="pr-open-item ob-item ob-shipping" data-outbox-row={s.title}>
      <RowMain title={s.title} text={s.text} shown={shown} row={row} />
      <div className="pr-open-meta">
        {reported && <span className="pr-open-chip ok">reported done</span>}
        <span>
          {s.note ||
            (s.step === "integrate" ? "merging back…" : s.step ? s.step.replace("_", " ") + "…" : "on its way")}
        </span>
        {group && <span>{group}</span>}
      </div>
      <div className="ik-item-start">
        {row && (
          <button type="button" className="test-btn" onClick={() => openSession(s.title)}>
            Open ↗
          </button>
        )}
      </div>
    </div>
  );
}

function ShippedRow({
  s,
  row,
  shown,
  group,
}: {
  s: OutboxShipped;
  row?: Instance;
  shown: (t: string) => string;
  group: string;
}) {
  const chip = shippedChip(s);
  return (
    <div className="pr-open-item ob-item ob-shipped" data-outbox-row={s.title}>
      <RowMain title={s.title} text={s.text} shown={shown} row={row} />
      <div className="pr-open-meta">
        <span className={"pr-open-chip " + chip.cls}>{chip.text}</span>
        {s.commit_subject && (
          <span>
            “{s.commit_subject}”{s.files ? " · " + s.files + (s.files === 1 ? " file" : " files") : ""}
          </span>
        )}
        {group && <span>{group}</span>}
      </div>
      <div className="ik-item-start">
        {s.pr_url && (
          <button type="button" className="test-btn" onClick={() => window.open(s.pr_url, "_blank")}>
            Review ↗
          </button>
        )}
        {s.verify ? (
          <button
            type="button"
            className="test-btn ob-verify"
            title="Its checklist is waiting in Verify"
            onClick={() => useUi.getState().openDialogFor("verify")}
          >
            Verify →
          </button>
        ) : null}
      </div>
    </div>
  );
}

function QueuedItem({ q, group }: { q: OutboxQueued; group: string }) {
  const [busy, setBusy] = useState(false);
  const id = q.run.id;
  const task = q.run.task || "";
  const go = async (what: string, verb: "start-now" | "skip", done: string) => {
    setBusy(true);
    try {
      await act(what, taskPath(id, task, verb), {}, done);
    } finally {
      setBusy(false);
    }
  };
  const label = q.text || q.ref || "queued line";
  return (
    <div className="pr-open-item ob-item ob-queued" data-outbox-row={task}>
      <div className="pr-open-main">
        <span className="pr-open-ref ob-ref">{q.ref || "task"}</span>
        <span className="pr-open-title">{q.text || ""}</span>
      </div>
      <div className="pr-open-meta">
        <span>waits for a free slot</span>
        {group && <span>{group}</span>}
      </div>
      <div className="ik-item-start">
        <button
          type="button"
          className="test-btn"
          disabled={busy || !task}
          title="Start it now, past the at-a-time limit (once)"
          onClick={() => go("Start now", "start-now", "Starting " + label)}
        >
          Start now
        </button>
        <button
          type="button"
          className="test-btn"
          disabled={busy || !task}
          title="Take it out of the group — it never starts"
          onClick={() => go("Remove", "skip", "Removed " + label)}
        >
          Remove
        </button>
      </div>
    </div>
  );
}

function SummaryCard({ s }: { s: OutboxSummary }) {
  return (
    <div className="ob-card ob-summary" data-outbox-summary={s.run}>
      <div className="ob-summary-head">
        <b>{s.name}</b>
        <span className="ik-group-detail">{s.state === "cancelled" ? "cancelled" : "finished"}</span>
        <button
          type="button"
          className="test-btn"
          onClick={async () => {
            if (await copyText(s.text_md)) toast("Copied the summary as Markdown");
          }}
        >
          Copy as Markdown
        </button>
      </div>
      <pre className="ob-summary-md">{s.text_md}</pre>
    </div>
  );
}
