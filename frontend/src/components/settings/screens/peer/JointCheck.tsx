/** The joint checklist on an active joint card: "do the halves agree?".
 *
 * The steps grouped by side (yours, then theirs), each with who checks it
 * (agent / person) and its result; Pass / Fail / Can't check on YOUR person's
 * steps; the verdict line; and Write the checklist (an inline editor, one
 * step per line with [theirs] / [human] tags), Ask my agent, Run my steps.
 * It never gates anything (docs/peer-link.md "Joint check — CONTRACT").
 *
 * The server's view carries its `link_id`, so the card that hosts this slot
 * only passes `check` and the peer's name; it refetches on `peer.joint_check`
 * for its link. */

import { useEffect, useState } from "react";
import {
  answerStep,
  askCheck,
  cellOf,
  fetchCheck,
  jointCheckChip,
  parseStepLines,
  runCheck,
  stepsToLines,
  verdictLine,
  verdictOf,
  verdictTone,
  writeCheck,
  type CheckResult,
  type CheckSide,
  type CheckStep,
  type JointCheckSummary,
  type JointCheckView,
} from "../../../../lib/peerCheck";
import { PEER_SCREEN } from "../../../../lib/peer";
import { openSettings } from "../../../../lib/openSettings";
import { usePeerLinks } from "../../../../state/queries";
import "./JointCheck.css";

const RESULT_TEXT: Record<string, string> = { pass: "Pass", fail: "Fail", blocked: "Can't check", "": "Not yet" };
const ANSWERS: Array<[CheckResult, string]> = [
  ["pass", "Pass"],
  ["fail", "Fail"],
  ["blocked", "Can't check"],
];

export function JointCheck({
  check: given,
  peerName,
  linkId: linkIdProp,
}: {
  check?: JointCheckView | null;
  peerName: string;
  linkId?: string;
}) {
  const [check, setCheck] = useState<JointCheckView | null>(given || null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  useEffect(() => setCheck(given || null), [given]);
  const linkId = linkIdProp || check?.link_id || "";

  // The other side's results, my agent's records, an ask running out.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev || !linkId) return;
    return ev.subscribe("peer.joint_check", (env) => {
      const d = (env.data || {}) as Record<string, unknown>;
      if (String(d.link_id || "") !== linkId) return;
      fetchCheck(linkId)
        .then((c) => c && setCheck(c))
        .catch(() => {});
    });
  }, [linkId]);

  if (!check || !linkId) return null;
  const name = (peerName || "").trim() || "your peer";
  const steps = check.steps || [];
  const verdict = verdictOf(check);
  const look = verdictTone(verdict);
  const asking = check.ask?.state === "asking";
  const myAgentSteps = steps.filter((s) => s.side === "mine" && s.actor === "agent").length;

  const act = async (label: string, fn: () => Promise<{ check: JointCheckView }>) => {
    setBusy(label);
    setError("");
    try {
      const r = await fn();
      if (r?.check) setCheck(r.check);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy("");
    }
  };

  const save = async () => {
    const parsed = parseStepLines(draft);
    if (!parsed.length) {
      setError("Write at least one step.");
      return;
    }
    if (await act("write", () => writeCheck(linkId, parsed))) setEditing(false);
  };

  const group = (side: CheckSide) => steps.filter((s) => s.side === side);
  const renderStep = (s: CheckStep) => {
    const n = steps.indexOf(s) + 1;
    const r = cellOf(check, s);
    const result = r?.result || "";
    const mineHuman = s.side === "mine" && s.actor === "human";
    return (
      <li key={s.id} className={"peer-joint-step peer-joint-step-" + (result || "blank")} data-step={s.id}>
        <span className="peer-joint-step-n">{n}</span>
        <span className="peer-joint-step-body">
          <span className="peer-joint-step-text">{s.text}</span>
          {s.expect ? <span className="peer-joint-step-expect">Expect: {s.expect}</span> : null}
          {r?.note ? <span className="peer-joint-step-note">{r.note}</span> : null}
        </span>
        <span className={"peer-joint-actor peer-joint-actor-" + s.actor}>{s.actor === "human" ? "person" : "agent"}</span>
        {mineHuman ? (
          <span className="peer-joint-answer" role="group" aria-label={"Answer step " + n}>
            {ANSWERS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={"test-btn peer-joint-answer-btn" + (result === value ? " on" : "")}
                aria-pressed={result === value}
                disabled={!!busy}
                onClick={() => act("answer", () => answerStep(linkId, s.id, result === value ? "" : value))}
              >
                {label}
              </button>
            ))}
          </span>
        ) : (
          <span className="peer-joint-step-result">{RESULT_TEXT[result] ?? "Not yet"}</span>
        )}
      </li>
    );
  };

  const sideBlock = (side: CheckSide, title: string) => {
    const list = group(side);
    if (!list.length) return null;
    const sha = (side === "mine" ? check.mine : check.theirs)?.tested_sha || "";
    return (
      <div className={"peer-joint-side peer-joint-side-" + side}>
        <div className="peer-joint-side-head">
          <span>{title}</span>
          {sha ? <code className="peer-joint-sha">checked at {sha.slice(0, 7)}</code> : null}
        </div>
        <ol className="peer-joint-steps">{list.map(renderStep)}</ol>
      </div>
    );
  };

  return (
    <div className="peer-joint-check" data-verdict={verdict}>
      <div className="peer-joint-check-head">
        <span className="set-label">Joint checklist</span>
        {steps.length ? (
          <span className="vchip peer-joint-verdict" data-verdict={verdict} data-tone={look.tone}>
            <span className="vchip-glyph" aria-hidden="true">
              {look.glyph}
            </span>
            {verdictLine(check, name)}
          </span>
        ) : null}
      </div>
      {check.theirs_unsupported ? (
        <p className="set-hint peer-joint-older">{name}'s MindFlock is older — their side can't report</p>
      ) : null}
      {asking ? <p className="set-hint peer-joint-asking">Your agent is writing the checklist…</p> : null}
      {check.ask?.state === "failed" && check.ask.error ? (
        <p className="set-hint peer-joint-ask-failed">{check.ask.error}</p>
      ) : null}

      {editing ? (
        <div className="peer-joint-editor">
          <textarea
            className="peer-joint-editor-text"
            rows={Math.min(12, Math.max(4, draft.split("\n").length + 1))}
            value={draft}
            aria-label="Joint checklist steps, one per line"
            placeholder={"Call GET /items -> a list of items\n[theirs] Run the client -> it prints them\n[theirs] [human] The printed list looks right"}
            onChange={(e) => setDraft(e.target.value)}
          />
          <p className="set-hint">
            One step per line. Add [theirs] for a step on {name}'s side and [human] for one a person checks; "-&gt;"
            starts what should happen. Saving clears every result.
          </p>
          <div className="peer-joint-actions">
            <button type="button" className="btn-primary" disabled={!!busy} onClick={save}>
              {busy === "write" ? "Saving…" : "Save checklist"}
            </button>
            <button type="button" className="test-btn" disabled={!!busy} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : steps.length ? (
        <>
          {sideBlock("mine", "Your side")}
          {sideBlock("theirs", name + "'s side")}
        </>
      ) : (
        <p className="set-hint">No steps yet. Write what shows the two halves agree, or ask your agent to.</p>
      )}

      {!editing ? (
        <div className="peer-joint-actions">
          <button
            type="button"
            className="test-btn"
            disabled={!!busy}
            onClick={() => {
              setDraft(stepsToLines(steps));
              setError("");
              setEditing(true);
            }}
          >
            Write the checklist
          </button>
          <button
            type="button"
            className="test-btn"
            disabled={!!busy || asking}
            onClick={() => act("ask", () => askCheck(linkId))}
          >
            {busy === "ask" ? "Asking…" : "Ask my agent"}
          </button>
          {myAgentSteps ? (
            <button type="button" className="test-btn" disabled={!!busy} onClick={() => act("run", () => runCheck(linkId))}>
              {busy === "run" ? "Starting…" : "Run my steps"}
            </button>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p className="set-hint peer-joint-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** The joint check's verdict on a shared session's sidebar row and pane head
 * (the Verify chip's look, `.vchip` — never `.stagechip`, which pane heads
 * hide). Nothing unless the session's link has a joint checklist. Reads the
 * link list the rail already polls (GET /api/peer); a click opens People. */
export function JointVerdictChip({
  linkId,
  peerName,
  where,
}: {
  linkId?: string;
  peerName?: string;
  where: "row" | "pane";
}) {
  const links = usePeerLinks().data?.links || [];
  if (!linkId) return null;
  const link = links.find((l) => l.link_id === linkId) as
    | { peer_name?: string; joint?: { check?: JointCheckSummary | null } | null }
    | undefined;
  const chip = jointCheckChip(link?.joint?.check, peerName || link?.peer_name || "");
  if (!chip) return null;
  return (
    <button
      type="button"
      className={"vchip vchip-" + where + " peer-joint-chip"}
      data-verdict={chip.verdict}
      data-tone={chip.tone}
      title={chip.title}
      aria-label={chip.title}
      onClick={(e) => {
        e.stopPropagation();
        openSettings(PEER_SCREEN);
      }}
      onDoubleClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <span className="vchip-glyph" aria-hidden="true">
        {chip.glyph}
      </span>
      {where === "pane" ? chip.short : null}
    </button>
  );
}
