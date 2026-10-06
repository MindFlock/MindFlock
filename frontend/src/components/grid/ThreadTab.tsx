/** The Thread tab (MindFlock MCP, from the UI): a session's family on one
 * page — the workers it forked, what each one needs from you, what passed
 * between them, and a composer that types into a member's prompt AS YOU.
 *
 * Two kinds of action, never mixed (the MCP UX contract):
 *  - DIRECT: a worker's dialog buttons (/answer, through AnswerStrip), and
 *    the composer (/send now, or /queue for when the session is free) — your
 *    words go in as you, never through the agents' mailbox;
 *  - PASTE: Check on workers, Wrap up and Merge into … render a named prompt
 *    and type it into the orchestrator's input box. Nothing runs until you
 *    press Enter there.
 *
 * The "Between sessions" log is read-only: `GET …/thread` never marks mail
 * read, so looking at it cannot eat a report an orchestrator is waiting for.
 * The pure half (rows, badge, log cards, send routing) is lib/thread.ts. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Instance, ThreadResponse } from "../../api/types";
import { instApi } from "../../api/client";
import { useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { effectiveActivity } from "../../lib/stage";
import { errMsg } from "../../lib/format";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { fetchDialog } from "../../lib/flockActions";
import { forkBlockReason, runPlaybook } from "../../lib/playbooks";
import {
  ALL_WORKERS,
  chipFor,
  claimComposeRequest,
  clockTime,
  codeSpans,
  composeChips,
  composePlaceholder,
  decidePrompt,
  deliveryText,
  diffText,
  familyOf,
  headerSummary,
  logEntries,
  mergeOlder,
  normThread,
  reportedCount,
  sendPlan,
  workerRows,
  workerStatus,
  type LogEntry,
  type LogFilter,
  type WorkerRow,
} from "../../lib/thread";
import { AnswerStrip } from "../AnswerStrip";
import { useRun } from "../../state/runs";
import { RunLeadPanel } from "./RunLeadPanel";

/** How often an open, visible Thread re-reads the family (events refresh it
 * sooner; this catches what no event announces, like a spawn). */
export const THREAD_POLL_MS = 8000;

/** The last thread body per session, so a remounted pane draws at once. */
const threadCache = new Map<string, ThreadResponse>();
/** The composer's unsent text per session — a grid move remounts the pane. */
const drafts = new Map<string, string>();
/** Who the composer is addressed to, per session — survives a remount the
 * same way the draft does. */
const toKeys = new Map<string, string>();

const nameOf = (t: string) => displayName(t);

function Spans({ text }: { text: string }) {
  return (
    <>
      {codeSpans(text).map((s, i) => (s.code ? <code key={i}>{s.text}</code> : <span key={i}>{s.text}</span>))}
    </>
  );
}

export function ThreadTab({ title, active }: { title: string; active: boolean }) {
  const { data: rowsData } = useInstances();
  const rows = useMemo(() => (rowsData ?? []) as Instance[], [rowsData]);
  const me = rows.find((r) => r.title === title && !r.device);
  const { parent, children } = useMemo(() => familyOf(title, rows), [title, rows]);
  // A split's lead (or a one-for-all group's): MindFlock merges its workers
  // back itself, so its Thread shows the plan, the pieces and the one PR —
  // not the paste buttons a hand-run family uses.
  const leadOf = me?.run?.role === "lead" ? me.run : null;
  const { data: leadRun } = useRun(leadOf?.id);
  const [data, setData] = useState<ThreadResponse | null>(threadCache.get(title) ?? null);
  const [filter, setFilter] = useState<LogFilter>("all");
  const [loadErr, setLoadErr] = useState("");
  const [busy, setBusy] = useState<string>("");

  // --- Loading -------------------------------------------------------------------
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const reload = useCallback(async () => {
    try {
      const body = normThread(await instApi<unknown>(title, "/thread?limit=50"), title);
      if (!live.current) return;
      // Keep older pages the user already pulled in: everything the shown
      // list holds before the new page's first item.
      const prev = threadCache.get(title);
      const firstId = body.items[0]?.id;
      const idx = prev && firstId ? prev.items.findIndex((i) => i.id === firstId) : -1;
      const next =
        idx > 0 ? { ...body, items: prev!.items.slice(0, idx).concat(body.items), more: prev!.more } : body;
      threadCache.set(title, next);
      setData((cur) => (JSON.stringify(cur) === JSON.stringify(next) ? cur : next));
      setLoadErr("");
    } catch (err) {
      if (live.current) setLoadErr(errMsg(err));
    }
  }, [title]);

  const older = async () => {
    const first = data?.items[0];
    if (!first) return;
    try {
      const body = normThread(
        await instApi<unknown>(title, "/thread?limit=50&before=" + encodeURIComponent(first.id)),
        title
      );
      const cur = threadCache.get(title) ?? data!;
      const next = { ...cur, items: mergeOlder(body.items, cur.items), more: body.more };
      threadCache.set(title, next);
      setData(next);
    } catch (err) {
      toast("Couldn't load older messages: " + errMsg(err));
    }
  };

  // While open and on screen: now, every few seconds, and whenever a family
  // member sends or receives a message.
  const familyKey = [title, parent, ...children.map((c) => c.title)].join("\n");
  useEffect(() => {
    if (!active) return;
    void reload();
    const tick = () => {
      if (document.visibilityState === "visible") void reload();
    };
    const timer = window.setInterval(tick, THREAD_POLL_MS);
    const ev = window.mindflock?.events;
    const fam = new Set(familyKey.split("\n").filter(Boolean));
    let soon: number | undefined;
    const off = ev?.subscribe("session.message", (env) => {
      const from = String((env.data as { from?: unknown } | undefined)?.from || "");
      if (!fam.has(env.session) && !fam.has(from)) return;
      clearTimeout(soon);
      soon = window.setTimeout(() => void reload(), 300);
    });
    return () => {
      clearInterval(timer);
      clearTimeout(soon);
      off?.();
    };
  }, [active, reload, familyKey]);

  // --- The family ---------------------------------------------------------------------
  const actOfRow = useCallback((r: Partial<Instance>) => effectiveActivity(r), []);
  const actOf = useCallback(
    (t: string) => {
      const r = rows.find((x) => x.title === t && !x.device);
      if (r) return effectiveActivity(r);
      return data?.members.find((m) => m.title === t)?.activity || "idle";
    },
    [rows, data]
  );
  const workers = useMemo(
    () => workerRows(children, data?.members ?? [], actOfRow),
    [children, data, actOfRow]
  );
  const reported = reportedCount(workers);
  const myName = nameOf(title);
  // Pastes go into THIS session's input box; they can't while it is on a
  // dialog (the paste would answer it).
  const pasteBlocked = me ? forkBlockReason(me) : "";

  const paste = async (key: string, id: string, args: Record<string, string> = {}, label?: string) => {
    if (busy) return;
    setBusy(key);
    try {
      await runPlaybook(title, id, args, label);
    } finally {
      if (live.current) setBusy("");
    }
  };

  // --- The composer -------------------------------------------------------------------
  const target = useUi((s) => (s.threadComposeTarget?.title === title ? s.threadComposeTarget : null));
  const [toKey, setToKeyState] = useState<string>(() => toKeys.get(title) || title);
  const setToKey = (to: string) => {
    toKeys.set(title, to);
    setToKeyState(to);
  };
  const [draft, setDraftState] = useState(drafts.get(title) ?? "");
  const setDraft = (v: string) => {
    drafts.set(title, v);
    setDraftState(v);
  };
  const [sending, setSending] = useState(false);
  const box = useRef<HTMLTextAreaElement | null>(null);
  const chips = composeChips(
    title,
    parent,
    children.map((c) => c.title),
    nameOf
  );
  const chip = chipFor(chips, toKey);
  const plan = sendPlan(chip.titles, actOf);

  // Every threadOpen (Ctrl+K S / T, the palette, Message…, a "No…" answer)
  // bumps `seq`: address the composer and take the caret, once the tab is
  // actually on screen (the pane may still be switching to it).
  const seq = target?.seq ?? 0;
  useEffect(() => {
    // Once per request (claimComposeRequest): not again when the tab comes
    // back on screen, nor when the pane remounts in another grid slot.
    if (!seq || !active || !target || !claimComposeRequest(title, seq)) return;
    setToKey(target.to || title);
    let tries = 0;
    let timer: number | undefined;
    const focus = () => {
      const el = box.current;
      if (el && el.offsetParent !== null) {
        el.focus();
        const n = el.value.length;
        el.setSelectionRange(n, n);
        return;
      }
      if (++tries < 20) timer = window.setTimeout(focus, 30);
    };
    timer = window.setTimeout(focus, 0);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seq, active]);

  const address = (to: string) => {
    setToKey(to);
    setTimeout(() => box.current?.focus(), 0);
  };

  const send = async (mode: "now" | "idle") => {
    const text = draft.trim();
    if (!text || sending) return;
    const plannedNow = mode === "now" ? plan.now : [];
    const plannedLater = mode === "now" ? plan.later : chip.titles;
    setSending(true);
    // "Now" is the server's call, not this tab's: its view of each agent is
    // a poll behind, and Enter typed into a permission prompt approves it.
    // `dialog_safe` re-checks the agent live and QUEUES instead when it is
    // on a prompt or at the usage limit.
    const results = await Promise.allSettled([
      ...plannedNow.map((t) =>
        instApi<{ queued?: boolean }>(t, "/send", { json: { text, dialog_safe: true } })
      ),
      ...plannedLater.map((t) => instApi(t, "/queue", { json: { text } })),
    ]);
    if (!live.current) return;
    setSending(false);
    // The Send button was disabled while sending, which dropped the focus
    // to the page — give it back to the box, where Delete edits text.
    setTimeout(() => box.current?.focus(), 0);
    const who = [...plannedNow, ...plannedLater];
    const failed = results
      .map((r, i) => (r.status === "rejected" ? nameOf(who[i]) + ": " + errMsg(r.reason) : ""))
      .filter(Boolean);
    if (failed.length === results.length) {
      toast("Couldn't send: " + failed.join("; "), { duration: 6000 });
      return;
    }
    const now: string[] = [];
    const later: string[] = [];
    results.forEach((r, i) => {
      if (r.status !== "fulfilled") return;
      const queuedByServer =
        i < plannedNow.length && !!(r.value as { queued?: boolean } | null)?.queued;
      (i < plannedNow.length && !queuedByServer ? now : later).push(who[i]);
    });
    setDraft("");
    const names = (ts: string[]) => (ts.length > 2 ? ts.length + " sessions" : ts.map(nameOf).join(" and "));
    const said: string[] = [];
    if (now.length) said.push("Sent to " + names(now));
    if (later.length)
      said.push(
        (now.length ? "queued for " : "Queued for ") +
          names(later) +
          (mode === "now" ? " — it gets it when it's free" : " — it runs when idle")
      );
    toast(said.join("; ") + (failed.length ? ". Failed: " + failed.join("; ") : ""), {
      duration: failed.length ? 6000 : 3000,
    });
  };

  const decide = async (worker: string) => {
    if (busy) return;
    setBusy("decide:" + worker);
    try {
      const dialog = await fetchDialog(worker).catch(() => null);
      // The worker's TITLE, not its alias: the orchestrator addresses
      // sessions by title, and an alias may be another session's title.
      await instApi(title, "/queue", { json: { text: decidePrompt(worker, dialog, me?.provider) } });
      toast(`Asked ${myName} to decide — queued, it runs when ${myName} is free`, { duration: 4000 });
    } catch (err) {
      toast(`Couldn't queue it for ${myName}: ` + errMsg(err), { duration: 6000 });
    } finally {
      if (live.current) setBusy("");
    }
  };

  // --- Render -------------------------------------------------------------------------
  const summary = headerSummary(workers);
  const entries = logEntries(data?.items ?? [], filter);
  const hasWorkers = workers.length > 0;
  const selfMember = data?.members.find((m) => m.role === "self");

  return (
    <div className="thread-root">
      <div className="thread-scroll">
        {leadOf ? (
          <RunLeadPanel title={title} me={me} run={leadRun} rows={rows} />
        ) : (
        <header className="thread-head">
          {hasWorkers ? (
            <>
              <h2 className="thread-title">{myName}'s workers</h2>
              <p className="thread-sub">
                {summary.map((p, i) => (
                  <span key={i}>
                    {i > 0 && p.cls !== "sha" ? " · " : ""}
                    {p.cls === "sha" ? <b className="th-sha">{p.text}</b> : <span className={p.cls ? "th-" + p.cls : undefined}>{p.text}</span>}
                  </span>
                ))}
                . Buttons below either answer directly or paste a prompt into {myName} — nothing is typed
                without you.
              </p>
            </>
          ) : parent ? (
            <>
              <h2 className="thread-title">{myName}</h2>
              <p className="thread-sub">
                Worker of <b>{nameOf(parent)}</b>
                {selfMember?.base_sha ? (
                  <>
                    {" "}
                    · forked from <b className="th-sha">{selfMember.base_sha.slice(0, 7)}</b>
                  </>
                ) : null}
                . Its reports and messages are below; {nameOf(parent)}'s Thread has the whole family.
              </p>
              {/* A worker on a dialog: answer it here too, as on the rail. */}
              <div className="th-self-answer">
                <AnswerStrip
                  title={title}
                  activity={actOf(title)}
                  variant="thread"
                  onRedirect={() => address(title)}
                />
              </div>
              <div className="thread-head-btns">
                <button type="button" className="th-btn" onClick={() => useUi.getState().threadOpen(parent)}>
                  Open {nameOf(parent)}'s Thread
                </button>
              </div>
            </>
          ) : (
            <>
              <h2 className="thread-title">{myName}</h2>
              <p className="thread-sub">
                No workers yet — use Split into parallel pieces… in the session's › menu. Or write to{" "}
                {myName} below; it is typed into its prompt as you.
              </p>
            </>
          )}
        </header>
        )}

        {hasWorkers && !leadOf && (
          <section className="thread-sec">
            <div className="thread-sec-head">
              <span className="thread-label">Workers</span>
              <span className="th-sp" />
              <button
                type="button"
                className="th-btn"
                disabled={!!busy || !!pasteBlocked}
                title={pasteBlocked || `Paste “Check on workers” into ${myName} — one line per worker; you press Enter`}
                onClick={() => paste("workers", "workers")}
              >
                Check on workers
              </button>
              <button
                type="button"
                className="th-btn"
                disabled={!!busy || !!pasteBlocked || !reported}
                title={
                  pasteBlocked ||
                  (reported
                    ? `Paste “Wrap up workers” into ${myName} — it merges each reported worker and runs the tests; you press Enter`
                    : "No worker has reported yet")
                }
                onClick={() => paste("wrapup", "wrapup")}
              >
                Wrap up ({reported} reported)
              </button>
            </div>
            <div className="thread-workers">
              {workers.map((w) => (
                <WorkerItem
                  key={w.title}
                  row={w}
                  parentName={myName}
                  busy={busy}
                  pasteBlocked={pasteBlocked}
                  onDecide={() => decide(w.title)}
                  onMerge={() =>
                    paste("merge:" + w.title, "wrapup", { only: w.title }, `Merge ${nameOf(w.title)} into ${myName}`)
                  }
                  onRedirect={() => address(w.title)}
                />
              ))}
            </div>
          </section>
        )}

        <section className="thread-sec">
          <div className="thread-sec-head">
            <span className="thread-label">Between sessions</span>
            <span className="th-sp" />
            <div className="th-seg" role="tablist" aria-label="Show">
              <button
                type="button"
                role="tab"
                aria-selected={filter === "all"}
                className={filter === "all" ? "on" : ""}
                onClick={() => setFilter("all")}
              >
                All
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={filter === "reports"}
                className={filter === "reports" ? "on" : ""}
                onClick={() => setFilter("reports")}
              >
                Reports
              </button>
            </div>
          </div>
          {data?.more && (
            <button type="button" className="th-older" onClick={older}>
              Show older
            </button>
          )}
          {entries.length ? (
            <div className="thread-log">
              {entries.map((e) => (
                <LogCard key={e.key} entry={e} />
              ))}
            </div>
          ) : (
            <p className="thread-empty">
              {loadErr
                ? "Couldn't load the thread: " + loadErr
                : !data
                  ? "Loading…"
                  : filter === "reports"
                    ? "No reports yet."
                    : "Nothing between sessions yet — spawns, messages and reports show up here."}
            </p>
          )}
        </section>
      </div>

      <div className="thread-compose">
        <div className="th-to">
          <span className="th-to-label">To</span>
          {chips.map((c) => (
            <button
              key={c.key}
              type="button"
              className={"th-chip" + (c.key === chip.key ? " on" : "") + (c.key === ALL_WORKERS ? " all" : "")}
              aria-pressed={c.key === chip.key}
              onClick={() => address(c.key)}
            >
              {c.label}
            </button>
          ))}
        </div>
        <textarea
          ref={box}
          className="thread-input"
          rows={3}
          placeholder={composePlaceholder(chip)}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void send("now");
            }
          }}
        />
        <div className="th-compose-foot">
          <span className="th-hint">
            {plan.later.length
              ? (plan.later.length === 1
                  ? nameOf(plan.later[0]) + " is on a prompt"
                  : plan.later.length + " are on a prompt") +
                " — queued for when it's free · Ctrl+Enter"
              : "Typed in as you, like the Queue tab · Ctrl+Enter sends now"}
          </span>
          <button
            type="button"
            className="th-btn th-idle"
            disabled={sending || !draft.trim()}
            title="Queue it — it runs when the session is next idle (the Queue tab's queue)"
            onClick={() => void send("idle")}
          >
            When idle
          </button>
          <button
            type="button"
            className="th-btn primary th-send"
            disabled={sending || !draft.trim()}
            title={
              plan.now.length
                ? "Type it into the prompt and press Enter, as you (Ctrl+Enter)"
                : "It is on a prompt — queue it for when it's free instead of typing into the dialog (Ctrl+Enter)"
            }
            onClick={() => void send("now")}
          >
            {plan.label}
          </button>
        </div>
      </div>
    </div>
  );
}

function WorkerItem({
  row,
  parentName,
  busy,
  pasteBlocked,
  onDecide,
  onMerge,
  onRedirect,
}: {
  row: WorkerRow;
  parentName: string;
  busy: string;
  pasteBlocked: string;
  onDecide(): void;
  onMerge(): void;
  onRedirect(): void;
}) {
  const st = workerStatus(row, parentName);
  const name = nameOf(row.title);
  const ds = diffText(row.diff);
  const reported = row.state === "done" || row.state === "blocked" || row.state === "failed";
  return (
    <div className={"th-worker is-" + row.state} data-title={row.title}>
      <div className="th-w-head">
        <span className={"th-dot " + st.cls} aria-hidden="true" />
        <button
          type="button"
          className="th-w-name"
          title={`Open ${name}`}
          onClick={() => selectSession(row.title)}
        >
          {name}
        </button>
        <span className={"th-w-word " + st.cls}>{st.word}</span>
        {st.detail && <span className="th-w-detail">· {st.detail}</span>}
        <span className="th-sp" />
        {ds && <span className="th-w-diff">{ds}</span>}
      </div>
      {row.state === "ask" && (
        <div className="th-w-body">
          <AnswerStrip
            title={row.title}
            activity={row.activity}
            variant="thread"
            onOpen={() => selectSession(row.title)}
            onRedirect={onRedirect}
          >
            <button
              type="button"
              className="fa-decide"
              disabled={!!busy}
              title={`Queue a prompt asking ${parentName} to look at this dialog and answer it if it is safe`}
              onClick={onDecide}
            >
              Let {parentName} decide
            </button>
          </AnswerStrip>
        </div>
      )}
      {reported && (
        <div className="th-w-body">
          {row.report?.summary && (
            <p className="th-w-summary">
              <Spans text={row.report.summary} />
            </p>
          )}
          <div className="th-w-acts">
            <button
              type="button"
              className="th-btn"
              title={`${name}'s Diff tab — its change since it forked`}
              onClick={() => {
                selectSession(row.title, { noKeyboard: true });
                useUi.getState().setLastTab(row.title, "diff");
              }}
            >
              Review diff
            </button>
            <button
              type="button"
              className="th-btn"
              disabled={!!busy || !!pasteBlocked}
              title={
                pasteBlocked ||
                `Paste “Wrap up ${name}” into ${parentName}: merge its branch, run the tests — you press Enter`
              }
              onClick={onMerge}
            >
              Merge into {parentName}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function LogCard({ entry }: { entry: LogEntry }) {
  const [all, setAll] = useState(false);
  const first = entry.items[0];
  const n = entry.items.length;
  const chipText =
    entry.kind === "spawn" ? "spawned" : entry.kind === "result" ? "result · " + (first.status || "sent") : "message";
  const chipCls =
    entry.kind === "result"
      ? first.status === "blocked" || first.status === "failed"
        ? " bad"
        : " ok"
      : entry.kind === "spawn"
        ? " spawn"
        : "";
  const foot =
    entry.kind === "spawn"
      ? first.base_sha
        ? "forked from " + first.base_sha.slice(0, 7)
        : ""
      : deliveryText(first.state, nameOf(first.to));
  return (
    <div className={"th-card k-" + entry.kind}>
      <div className="th-card-head">
        <b>{nameOf(entry.from)}</b>
        <span className="th-arrow">→</span>
        <b className="th-card-to">{entry.to.map(nameOf).join(", ")}</b>
        <span className={"th-kind" + chipCls}>{chipText}</span>
        <span className="th-sp" />
        <span className="th-time" title={new Date(entry.ts * 1000).toLocaleString()}>
          {clockTime(entry.ts)}
        </span>
      </div>
      {all && n > 1 ? (
        <div className="th-card-many">
          {entry.items.map((it) => (
            <div key={it.id} className="th-card-one">
              <b>{nameOf(it.to)}</b>
              <p className="th-card-text">{it.text ? <Spans text={it.text} /> : <i>no prompt recorded</i>}</p>
            </div>
          ))}
        </div>
      ) : first.text ? (
        <p className="th-card-text">
          <Spans text={first.text} />
        </p>
      ) : entry.kind === "spawn" ? null : (
        <p className="th-card-text">
          <i>(empty)</i>
        </p>
      )}
      {(foot || n > 1) && (
        <div className="th-card-foot">
          <span>{foot}</span>
          <span className="th-sp" />
          {n > 1 && (
            <button type="button" className="th-btn" onClick={() => setAll((v) => !v)}>
              {all ? "Show less" : "Show all " + n}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
