/** The lead's half of the Thread tab, for a split or a one-for-all group
 * (SPEC §7.C.4): the plan card while the lead's pieces wait for your OK, the
 * ship card once everything is merged back and checked, and one row per
 * piece saying where it is.
 *
 * MindFlock does the work between the two clicks that are yours: it starts
 * the workers, fences each one to its paths, merges each back into the lead's
 * branch as it finishes, hands a conflict to the lead, runs the check on the
 * merged branch, and opens the ONE PR when you say so. Nothing here pastes a
 * prompt into an agent, and nothing asks through a browser dialog (Electron
 * has none): editing a piece and the note for a different split are inline
 * rows. The sentences are lib/splitRun.ts.
 *
 * Such a group has no header on the rail (it is a family under its lead), so
 * this panel is also its home for what a header's ⋯ holds: a queued piece's
 * Start now / Remove, and a finished group's "Copy summary". */

import { useState } from "react";
import type { Instance, PlanPiece, RunDTO, RunTask } from "../../api/types";
import { api, ApiError } from "../../api/client";
import { displayName, useUi } from "../../state/store";
import { refreshRuns } from "../../state/runs";
import { refreshInstances } from "../../state/queries";
import { effectiveActivity } from "../../lib/stage";
import { errMsg } from "../../lib/format";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { copyText } from "../../lib/clipboard";
import { runAction, runPath, taskPath } from "../../lib/runsApi";
import { RUN_DONE_STATES } from "../../lib/runs";
import { summaryText } from "../outbox/outbox";
import { errorPop } from "../../lib/errorPop";
import {
  editPlan,
  laneNote,
  leadSubline,
  modeChoices,
  startedText,
  memberTasks,
  pathsText,
  pieceLabel,
  pieceStatus,
  planProblems,
  planRows,
  releaseCard,
  checkLine,
  releaseChoices,
  releaseOutcome,
  startWorkersLabel,
} from "../../lib/splitRun";
import type { SplitMode } from "../../api/types";
import { AnswerStrip } from "../AnswerStrip";

const nameOf = (t: string) => displayName(t);

export function RunLeadPanel({
  title,
  me,
  run,
  rows,
}: {
  title: string;
  me: Instance | undefined;
  run: RunDTO | null | undefined;
  rows: Instance[];
}) {
  const myName = nameOf(title);
  const [busy, setBusy] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [form, setForm] = useState({ title: "", prompt: "", paths: "" });
  const [problems, setProblems] = useState<string[]>([]);
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  // Where the pieces run — picked on the plan card, separate worktrees first.
  const [mode, setMode] = useState<Exclude<SplitMode, "">>("worktrees");

  if (!run) {
    return (
      <header className="thread-head">
        <h2 className="thread-title">{myName}'s workers</h2>
        <p className="thread-sub">Loading its group…</p>
      </header>
    );
  }

  const id = run.id;
  const split = !!run.split;
  const act = async (key: string, what: string, path: string, body: unknown, done: string) => {
    if (busy) return false;
    setBusy(key);
    try {
      return await runAction(what, runPath(id, path), body, done);
    } finally {
      setBusy("");
    }
  };

  // --- The plan ----------------------------------------------------------------
  const pieces: PlanPiece[] = run.plan?.pieces || [];
  const plan = planRows(pieces);
  const startEdit = (i: number) => {
    setEditing(i);
    setProblems([]);
    setForm({
      title: plan[i].title,
      prompt: plan[i].prompt,
      paths: plan[i].paths.join(", "),
    });
  };
  const saveEdit = async () => {
    if (editing === null || busy) return;
    setBusy("edit");
    try {
      await api(runPath(id, "/plan"), {
        json: {
          pieces: editPlan(pieces, editing, form),
          why: run.plan?.why || "",
        },
      });
      setEditing(null);
      setProblems([]);
      toast("Plan updated");
    } catch (err) {
      setProblems(err instanceof ApiError && err.status === 422 ? planProblems(err.body) : [errMsg(err)]);
    } finally {
      setBusy("");
      void refreshRuns();
    }
  };
  const choices = modeChoices(run, myName);
  const chosen = choices.find((c) => c.mode === mode) || choices[0];
  const approve = async () => {
    if (busy) return;
    setBusy("approve");
    try {
      const res = await api<{ run?: RunDTO }>(runPath(id, "/plan/approve"), { json: { mode } });
      const lead = res?.run?.lead?.title || "";
      if (res?.run?.origin && lead && lead !== title) {
        // Separate worktrees for a lead that works in its own folder (or on
        // its trunk): the pieces got a NEW lead — its Thread is the group's.
        toast(
          startedText(plan.length, "worktrees", myName) +
            " under " +
            nameOf(lead) +
            ", a new lead from " +
            myName +
            "'s last commit — " +
            myName +
            " is left as it is",
          { duration: 7000 },
        );
        useUi.getState().threadOpen(lead);
      } else toast(startedText(plan.length, mode, myName));
    } catch (err) {
      errorPop("Start the workers failed", errMsg(err));
    } finally {
      setBusy("");
      void refreshRuns();
      void refreshInstances();
    }
  };
  // "Start a branch here first": only on this click, in the lead's folder.
  const startBranch = () =>
    act("branch", "Start a branch here", "/lead/branch", {}, "Started a branch in " + myName + "'s folder");
  const reject = async () => {
    const ok = await act(
      "reject",
      "Ask for a different split",
      "/plan/reject",
      { note: note.trim() },
      "Asked " + myName + " for a different split",
    );
    if (ok) {
      setAsking(false);
      setNote("");
    }
  };

  // --- The ship card -------------------------------------------------------------
  const card = releaseCard(run);
  const outcome = releaseOutcome(run);
  const lane = laneNote(me?.lane);
  const pushOnly = run.policy?.lane === "push";
  const release = (merge: boolean) =>
    act(
      merge ? "merge" : "release",
      pushOnly ? "Push the branch" : "Open the PR",
      "/release",
      { merge_when_green: merge },
      merge ? "Opening the PR — it merges once checks pass" : pushOnly ? "Pushing the branch" : "Opening the PR",
    );
  const reviewDiff = () => {
    selectSession(title, { noKeyboard: true });
    useUi.getState().setLastTab(title, "diff");
  };
  const showShip = ["checking", "release_ready", "releasing", "done", "done_with_failures"].includes(run.state);

  const tasks = memberTasks(run);
  const sub = leadSubline(run, myName);
  const finished = RUN_DONE_STATES.has(run.state);
  // The run record keeps its summary for as long as it keeps the run.
  const summary = finished ? summaryText(null, id, run) : "";
  const pieceAct = (t: RunTask, verb: "start-now" | "skip") => {
    if (busy) return;
    setBusy(verb + ":" + t.id);
    const name = pieceLabel(t, run).name;
    void runAction(
      verb === "skip" ? "Remove" : "Start now",
      taskPath(id, t.id, verb),
      {},
      (verb === "skip" ? "Removed " : "Starting ") + name,
    ).finally(() => setBusy(""));
  };
  const noTools = me?.mcp_attached === false && split && (run.state === "planning" || run.state === "plan_ready");

  return (
    <>
      <header className="thread-head">
        <h2 className="thread-title">{myName}'s workers</h2>
        <p className="thread-sub rb-sub-line">
          {sub.map((p, i) =>
            p.b ? (
              <b key={i} className={p.cls ? "th-" + p.cls : undefined}>
                {p.text}
              </b>
            ) : (
              <span key={i} className={p.cls ? "th-" + p.cls : undefined}>
                {p.text}
              </span>
            ),
          )}
        </p>
        {run.mode === "same_folder" && !!(run.stray?.paths?.length || run.stray?.commits?.length) && (
          <p className="thread-sub th-bad rb-warn" data-stray="">
            {run.stray?.paths?.length
              ? "Changes no piece owns in " +
                myName +
                "'s folder: " +
                run.stray.paths.slice(0, 4).join(", ") +
                (run.stray.paths.length > 4 ? " (+" + (run.stray.paths.length - 4) + " more)" : "") +
                " — no piece's commit takes them; commit or discard them yourself."
              : ""}
            {run.stray?.commits?.length
              ? " A commit no piece made is on the group's branch (" +
                run.stray.commits.slice(0, 3).map((c) => c.slice(0, 9)).join(", ") +
                ") — it ships with the PR unless you undo it."
              : ""}
          </p>
        )}
        {finished && (
          <div className="thread-head-btns">
            <button
              type="button"
              className="th-btn rb-copy-summary"
              disabled={!summary}
              title={summary ? "Copy what this group did, as Markdown" : "MindFlock has no summary for this group"}
              onClick={async () => {
                if (!summary) return;
                toast((await copyText(summary)) ? "Copied the summary as Markdown" : "Couldn't copy the summary");
              }}
            >
              Copy summary
            </button>
          </div>
        )}
        {noTools && (
          <p className="thread-sub th-bad rb-warn">
            Restart the lead to give it the MindFlock tools — it proposes the pieces with them.
          </p>
        )}
      </header>

      {run.state === "plan_ready" && (
        <section className="thread-sec">
          <div className="thread-sec-head">
            <span className="thread-label">Plan · {plan.length}</span>
            <span className="th-sp" />
            <span className="rb-note">proposed by {myName} · each piece may edit only its paths</span>
          </div>
          <div className="rb-card rb-plan">
            {run.plan?.why && <p className="rb-why">{run.plan.why}</p>}
            {plan.map((p, i) =>
              editing === i ? (
                <div key={i} className="rb-piece-edit">
                  <label>
                    <span>Name</span>
                    <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
                  </label>
                  <label>
                    <span>Prompt</span>
                    <textarea
                      rows={3}
                      value={form.prompt}
                      onChange={(e) => setForm({ ...form, prompt: e.target.value })}
                    />
                  </label>
                  <label>
                    <span>Only here</span>
                    <input
                      value={form.paths}
                      placeholder="quickpay/auth/tokens*, quickpay/auth/tests/test_tokens.py"
                      onChange={(e) => setForm({ ...form, paths: e.target.value })}
                    />
                  </label>
                  <div className="rb-btns">
                    <button type="button" className="th-btn primary" disabled={!!busy} onClick={() => void saveEdit()}>
                      Save
                    </button>
                    <button type="button" className="th-btn" disabled={!!busy} onClick={() => setEditing(null)}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div key={i} className="rb-plan-row">
                  <div className="rb-plan-main">
                    <b>{p.title}</b>
                    <span className="rb-plan-prompt">{p.prompt}</span>
                    <span className="rb-only">
                      only here: <code>{p.pathsText || "—"}</code>
                    </span>
                  </div>
                  <button
                    type="button"
                    className="th-btn"
                    disabled={!!busy || editing !== null}
                    onClick={() => startEdit(i)}
                  >
                    Edit
                  </button>
                </div>
              ),
            )}
            {problems.length > 0 && (
              <ul className="rb-problems">
                {problems.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            )}
            <fieldset className="rb-modes" disabled={!!busy || editing !== null}>
              <legend>Where the pieces run</legend>
              {choices.map((c) => (
                <label key={c.mode} className={"rb-mode" + (mode === c.mode ? " on" : "")} data-mode={c.mode}>
                  <input
                    type="radio"
                    name={"rb-mode-" + id}
                    value={c.mode}
                    checked={mode === c.mode}
                    onChange={() => setMode(c.mode)}
                  />
                  <span className="rb-mode-text">
                    <b>{c.label}</b>
                    <span className="rb-mode-hint">{c.hint}</span>
                  </span>
                </label>
              ))}
            </fieldset>
            {chosen.blocked && (
              <div className="rb-btns rb-mode-block">
                <span className="rb-status th-bad">{chosen.blocked}.</span>
                <button type="button" className="th-btn primary" disabled={!!busy} onClick={() => void startBranch()}>
                  Start a branch here first
                </button>
                <button type="button" className="th-btn" disabled={!!busy} onClick={() => setMode("worktrees")}>
                  Use separate worktrees
                </button>
              </div>
            )}
            {asking ? (
              <div className="rb-ask">
                <input
                  value={note}
                  autoFocus
                  placeholder={"What should " + myName + " change? (optional)"}
                  onChange={(e) => setNote(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void reject();
                    if (e.key === "Escape") setAsking(false);
                  }}
                />
                <button type="button" className="th-btn primary" disabled={!!busy} onClick={() => void reject()}>
                  Send to {myName}
                </button>
                <button type="button" className="th-btn" onClick={() => setAsking(false)}>
                  Cancel
                </button>
              </div>
            ) : (
              <div className="rb-btns">
                <button
                  type="button"
                  className="th-btn primary"
                  disabled={!!busy || editing !== null || !plan.length || !!chosen.blocked}
                  title={chosen.hint}
                  onClick={() => void approve()}
                >
                  {startWorkersLabel(plan.length)}
                </button>
                <button type="button" className="th-btn" disabled={!!busy} onClick={() => setAsking(true)}>
                  Ask for a different split
                </button>
              </div>
            )}
          </div>
        </section>
      )}

      {showShip && (
        <section className="thread-sec">
          <div className="thread-sec-head">
            <span className="thread-label">Release · one PR</span>
            <span className="th-sp" />
            {lane && <span className="rb-note">{lane}</span>}
          </div>
          <div className="rb-card rb-ship">
            <dl className="rb-grid">
              <dt>Title</dt>
              <dd className="rb-strong">{card.title}</dd>
              <dt>Into</dt>
              <dd className="rb-mono">
                {card.into}
                {card.stat && <span className="rb-stat"> · {card.stat}</span>}
              </dd>
              <dt>Commits</dt>
              <dd>{card.commits}</dd>
              <dt>Body</dt>
              <dd>{card.body}</dd>
              {checkLine(run) && (
                <>
                  <dt>Check</dt>
                  <dd>{checkLine(run)}</dd>
                </>
              )}
            </dl>
            {run.state === "checking" ? (
              run.check?.state === "failed" ? (
                <div className="rb-btns">
                  <span className="rb-status th-bad">
                    The check failed on the merged branch
                    {run.check.summary ? ": " + run.check.summary : ""}
                  </span>
                  <span className="th-sp" />
                  <button type="button" className="th-btn" onClick={reviewDiff}>
                    Review the diff
                  </button>
                  <button
                    type="button"
                    className="th-btn primary"
                    disabled={!!busy}
                    onClick={() => void act("check", "Run the check", "/check", {}, "Running the check again")}
                  >
                    Run the check again
                  </button>
                </div>
              ) : (
                <div className="rb-btns">
                  <span className="rb-status">Running the check on the merged branch…</span>
                </div>
              )
            ) : run.state === "release_ready" ? (
              <div className="rb-btns">
                {run.release?.local_origin && (
                  <span className="rb-status th-idle">
                    Its origin is {run.release.local_origin} — a folder on this machine, not GitHub: releasing pushes there,
                    and no PR can be opened
                  </span>
                )}
                {releaseChoices(run.policy?.lane, run.release?.local_origin).map((c) => (
                  <button
                    key={c.label}
                    type="button"
                    className={"th-btn" + (c.primary ? " primary" : "")}
                    title={c.title}
                    disabled={!!busy}
                    onClick={() => void release(c.merge)}
                  >
                    {c.label}
                  </button>
                ))}
                <button type="button" className="th-btn" onClick={reviewDiff}>
                  Review the diff
                </button>
              </div>
            ) : outcome ? (
              <div className="rb-btns">
                <span className={"rb-status th-" + outcome.cls}>{outcome.text}</span>
                {outcome.url && (
                  <a className="th-btn" href={outcome.url} target="_blank" rel="noreferrer">
                    {outcome.link}
                  </a>
                )}
                {(run.release?.state === "handoff" || (run.release?.state === "done" && !!run.release.local_origin)) &&
                  run.release.title && (
                  // No compare page (a non-GitHub origin, no gh, no token):
                  // the PR MindFlock built is yours to paste wherever it goes.
                  <>
                    <button
                      type="button"
                      className="th-btn"
                      onClick={() =>
                        void copyText(run.release?.title || "").then((ok) => toast(ok ? "PR title copied" : "Couldn't copy"))
                      }
                    >
                      Copy title
                    </button>
                    <button
                      type="button"
                      className="th-btn"
                      onClick={() =>
                        void copyText(run.release?.body || "").then((ok) => toast(ok ? "PR body copied" : "Couldn't copy"))
                      }
                    >
                      Copy body
                    </button>
                  </>
                )}
                <span className="th-sp" />
                <button type="button" className="th-btn" onClick={reviewDiff}>
                  Review the diff
                </button>
              </div>
            ) : null}
          </div>
        </section>
      )}

      {tasks.length > 0 && (
        <section className="thread-sec">
          <div className="thread-sec-head">
            <span className="thread-label">
              {split ? "Pieces" : "Lines"} · {tasks.length}
            </span>
            <span className="th-sp" />
            <span className="rb-note">
              {run.mode === "same_folder"
                ? "planned by the lead · started and committed by MindFlock, in " + myName + "'s folder"
                : split
                  ? "planned by the lead · started and merged by MindFlock"
                  : "started and merged by MindFlock"}
            </span>
          </div>
          <div className="thread-workers rb-pieces">
            {tasks.map((t) => {
              const st = pieceStatus(t, myName, run.mode);
              const lbl = pieceLabel(t, run);
              const row = rows.find((r) => r.title === t.title && !r.device);
              const activity = row ? effectiveActivity(row) : "";
              const commit = (t.commits || [])[0] || "";
              const paths = pathsText(t.paths);
              return (
                <div key={t.id} className={"th-worker rb-piece is-" + st.cls} data-title={t.title}>
                  <div className="th-w-head">
                    <span className={"th-dot " + st.cls} aria-hidden="true" />
                    <button
                      type="button"
                      className="rb-pname"
                      disabled={!row}
                      title={row ? "Open " + nameOf(t.title) : "Not started yet"}
                      onClick={() => selectSession(t.title)}
                    >
                      <b>{lbl.name}</b>
                      {lbl.what && <span> — {lbl.what}</span>}
                    </button>
                    <span className="th-sp" />
                    <span className={"th-w-word " + st.cls}>{st.word}</span>
                    {st.detail && (
                      <span className={"th-w-detail" + (st.cls === "ok" ? " th-ok" : "")}>{st.detail}</span>
                    )}
                    {t.state === "queued" && !finished && (
                      <>
                        <button
                          type="button"
                          className="th-btn rb-start-now"
                          disabled={!!busy}
                          title="Start it now, past the at-a-time limit (once)"
                          onClick={() => pieceAct(t, "start-now")}
                        >
                          Start now
                        </button>
                        <button
                          type="button"
                          className="th-btn rb-remove"
                          disabled={!!busy}
                          title="Take it out of the group — it never starts"
                          onClick={() => pieceAct(t, "skip")}
                        >
                          Remove
                        </button>
                      </>
                    )}
                  </div>
                  {(paths || commit) && (
                    <div className="rb-piece-sub">
                      {paths && (
                        <>
                          only here: <code>{paths}</code>
                        </>
                      )}
                      {paths && commit && " · "}
                      {commit && <span>“{commit}”</span>}
                    </div>
                  )}
                  {activity === "clarify" && (
                    <div className="th-w-body">
                      <AnswerStrip
                        title={t.title}
                        activity={activity}
                        variant="thread"
                        onOpen={() => selectSession(t.title)}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}
    </>
  );
}
