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
 * rows. The sentences are lib/splitRun.ts. */

import { useState } from "react";
import type { Instance, PlanPiece, RunDTO } from "../../api/types";
import { api, ApiError } from "../../api/client";
import { displayName, useUi } from "../../state/store";
import { refreshRuns } from "../../state/runs";
import { refreshInstances } from "../../state/queries";
import { effectiveActivity } from "../../lib/stage";
import { errMsg } from "../../lib/format";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { copyText } from "../../lib/clipboard";
import { runAction, runPath } from "../../lib/runsApi";
import {
  editPlan,
  laneNote,
  leadSubline,
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
  const approve = () =>
    act(
      "approve",
      "Start the workers",
      "/plan/approve",
      {},
      startWorkersLabel(plan.length).replace(/^Start/, "Starting") + " — each fenced to its paths",
    ).then((ok) => ok && void refreshInstances());
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
                  disabled={!!busy || editing !== null || !plan.length}
                  title="MindFlock starts one worker per piece, forked from the lead's last commit, each fenced to its paths"
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
            <span className="thread-label">Ship · one PR</span>
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
              {split ? "planned by the lead · started and merged by MindFlock" : "started and merged by MindFlock"}
            </span>
          </div>
          <div className="thread-workers rb-pieces">
            {tasks.map((t) => {
              const st = pieceStatus(t, myName);
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
