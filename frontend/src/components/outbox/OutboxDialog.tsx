/** The Outbox — a read-only log of what's on its way out, and what shipped
 * today (SPEC §7.C.3). It lives as the third tab of Customize: an extra you
 * look at, not a place that needs you.
 *
 * Everything that WAITS ON YOU — an answer, an approval with its commit
 * message, a stuck group line, a spent budget, a lead's plan or PR — is the
 * bell's (WaitingRow.tsx renders those rows there). This tab only says how
 * many there are and opens the bell, so the two can never list one thing
 * twice. What is left here is MindFlock's own work: "Shipping now" (read-only),
 * "Shipped today" (including closed sessions — nowhere else keeps that), a
 * group's queued lines and a finished group's summary.
 *
 * Chips filter one response (outbox.ts says why); a chip per group with live
 * rows, plus "On their own" once any group exists — and no chip strip at all
 * while there is only "All". Every action posts at once and reports failure
 * in the error card — no `window.prompt` / `confirm` (Electron has neither). */

import { useEffect, useMemo, useState } from "react";
import type { OutboxQueued, OutboxShipped, OutboxShipping, OutboxSummary, Instance } from "../../api/types";
import { useInstances } from "../../state/queries";
import { refreshRuns, useOutbox, useRuns } from "../../state/runs";
import { useUi } from "../../state/store";
import { copyText } from "../../lib/clipboard";
import { errMsg } from "../../lib/format";
import { windowName } from "../../lib/windowName";
import { toast } from "../../lib/toast";
import { useToggleSet, WorkGroup } from "../intake/kit";
import { outboxTabs, shippedChip, viewCount, viewFor, type OutboxTabKey } from "./outbox";
import { openSession, RowMain } from "./WaitingRow";
import { runAction as act, taskPath } from "../../lib/runsApi";

/** Hand the user to the bell, where what waits on them lives. */
function openBell() {
  useUi.getState().closeDialog();
  document.dispatchEvent(new CustomEvent("mf-open-bell"));
}

/** The Customize dialog's Outbox tab. `target` is the dialog target: a run id
 * (a bell or toast row, a group header's "Open in the Outbox") or "own". */
export function OutboxPanel({ target }: { target: string | null }) {
  // Rows are named by windowName, which reads renames from the store: keep a
  // subscription so a rename repaints the tab.
  useUi((s) => s.aliases);
  const [tab, setTab] = useState<OutboxTabKey>("all");
  const { data, error, isFetching } = useOutbox();
  const { data: instances = [] } = useInstances();
  const { data: runs } = useRuns();
  // Sections default OPEN (the rows are why you came); the set holds the ones
  // you folded, per device.
  const folds = useToggleSet("mf_outbox_folded", true);

  // A bell row or a group header opens the Outbox on its group.
  useEffect(() => {
    setTab(target || "all");
    void refreshRuns();
  }, [target]);

  const byTitle = useMemo(() => new Map(instances.map((i) => [i.title, i])), [instances]);
  const rowOf = (t: string) => byTitle.get(t);
  const runName = (id: string) => runs?.find((r) => r.id === id)?.name || "";

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
  // One name per session: the rail's (a rename, else its pipeline label) —
  // the bell's rows and this tab's name a window the same way.
  const shown = windowName;
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
  const waiting = view.waiting.length;

  return (
    <div id="outbox-panel">
      <div className="cz-tab-head">
        <span className="ik-subtitle">What's on its way out, and what shipped today</span>
        {isFetching && data ? <span className="ob-fetch">refreshing…</span> : null}
      </div>
      {waiting > 0 && (
        <div className="ob-to-bell">
          <span>
            {waiting} waiting on you — in the bell
          </span>
          <button type="button" className="test-btn" onClick={openBell}>
            Open the bell
          </button>
        </div>
      )}
      {tabs.length >= 2 && (
        <nav id="outbox-tabs" aria-label="Outbox groups">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              className={"ob-chip" + (tab === t.key ? " active" : "")}
              data-outbox-tab={t.key}
              aria-pressed={tab === t.key}
              onClick={() => setTab(t.key)}
            >
              {t.label}
              {t.count > 0 && <span className="ob-chip-count">{t.count}</span>}
            </button>
          ))}
        </nav>
      )}
      <div id="outbox-body">
        {data === null ? (
          <p className="repo-empty">
            This MindFlock server has no Outbox yet — update it, then sessions you start
            together (and any session with ⏩ Fast-track on) show up here.
          </p>
        ) : error && !data ? (
          <p className="repo-empty">Could not load the Outbox: {errMsg(error)}</p>
        ) : !data ? (
          <p className="repo-empty">Loading…</p>
        ) : (
          <div className="ik-groups ob-groups">
            {total === 0 && !view.summaries.length && (
              <p className="repo-empty">
                Nothing is on its way out. Sessions show up here once ⏩ Fast-track carries them,
                or when you start several together from New.
              </p>
            )}
            {/* A group's own chip (where its finish notification lands) is
                topped by its summary; under All the summaries come last. */}
            {tab !== "all" && view.summaries.map((s) => <SummaryCard key={"sum:" + s.run} s={s} />)}
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
