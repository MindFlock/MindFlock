/** A joint task on a link card: two people, two repos, one feature. One
 * per link. What the card shows follows `jointPhase`:
 *
 *  - none            Start a joint task → the form (brief, my folder, branch,
 *                    agent, a starter contract) → POST …/joint
 *  - proposed-by-me  "Proposed to <name> — waiting for them to accept"
 *  - proposed-by-peer their brief + contract, Accept with… (my folder, branch,
 *                    agent) → POST …/joint/accept, or Decline → …/joint/cancel
 *  - active          the brief, both halves (Open session / Watch their
 *                    session), the Contract panel, the Mirror row, the joint
 *                    checklist when there is one, Done and Cancel
 *  - ended           done or cancelled, and Start a joint task again
 *
 * `JointSection` fetches the whole task (GET …/joint) whenever the screen's
 * summary of it changes; `JointView` draws one phase from props (tested). */

import { useEffect, useState } from "react";
import { api } from "../../../../api/client";
import { selectSession } from "../../../../lib/sessionActions";
import { useUi } from "../../../../state/store";
import { InlineConfirm } from "../../useSettings";
import { PathField } from "../../../PathField";
import {
  baseName,
  CONTRACT_TEMPLATE,
  jointPath,
  jointPhase,
  type PeerJoint,
  type PeerLink,
} from "../../../../lib/peer";
import { AgentSelect } from "./AgentSelect";
import { ContractMarkdown } from "./ContractMarkdown";
import { ContractPanel } from "./JointContract";
import { JointCheck } from "./JointCheck";
import { MirrorRow } from "./MirrorRow";
import { errText, useAct } from "./useAct";

type LinkBits = Pick<
  PeerLink,
  "link_id" | "peer_name" | "connected" | "sas_verified" | "joint" | "mirror" | "their_share"
>;

export interface JointProps {
  link: LinkBits;
  agents: string[];
  sandbox: boolean;
  tick: number;
  reload(): void;
}

/** The fetching half: the summary in GET /api/peer says whether there is a
 * task and when it changed; the whole task comes from GET …/joint. */
export function JointSection(props: JointProps) {
  const { link, tick } = props;
  const [joint, setJoint] = useState<PeerJoint | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const sum = link.joint;
  // null: the server says there is none. undefined: an older summary — ask.
  const key = sum === null ? "none" : sum ? [sum.id, sum.state, sum.contract_rev, sum.open_proposals, sum.mine_shared, sum.theirs_shared].join(":") : "ask";
  useEffect(() => {
    if (key === "none") {
      setJoint(null);
      return;
    }
    let live = true;
    api<{ joint: PeerJoint | null }>(jointPath(link.link_id))
      .then((r) => {
        if (!live) return;
        setJoint(r.joint || null);
        setLoadErr("");
      })
      .catch((e) => live && setLoadErr(errText(e)));
    return () => {
      live = false;
    };
  }, [key, tick, link.link_id]);
  return <JointView {...props} joint={key === "none" ? null : joint} loadErr={loadErr} />;
}

export function JointView(props: JointProps & { joint: PeerJoint | null; loadErr?: string; now?: number }) {
  const { link, joint, loadErr } = props;
  const phase = jointPhase(joint);
  return (
    <div className="peer-joint" data-tour="people-joint" data-joint-phase={phase}>
      {phase === "none" && <JointStart {...props} />}
      {phase === "proposed-by-me" && joint && <JointWaiting {...props} joint={joint} />}
      {phase === "proposed-by-peer" && joint && <JointProposed {...props} joint={joint} />}
      {phase === "active" && joint && <JointActive {...props} joint={joint} />}
      {phase === "ended" && joint && (
        <>
          <p className="set-hint peer-joint-ended">
            {joint.state === "done" ? "The joint task is done" : "The joint task was cancelled"}: {joint.brief}
          </p>
          <JointStart {...props} again />
        </>
      )}
      {loadErr && <p className="error">Couldn't load the joint task with {link.peer_name}: {loadErr}</p>}
    </div>
  );
}

/** Why Start can't go yet, as a sentence — or "" when it can. */
export function jointStartBlocker(link: Pick<PeerLink, "peer_name" | "connected" | "sas_verified">): string {
  if (!link.sas_verified) return "Check the safety number with " + link.peer_name + " first — then you can start a joint task.";
  if (!link.connected) return link.peer_name + " is offline — start a joint task once they're back.";
  return "";
}

/** `formOpen` opens the form straight away (a static render, a test). */
export function JointStart(props: JointProps & { again?: boolean; formOpen?: boolean }) {
  const { link, agents, sandbox, reload, again } = props;
  const { busy, err, act } = useAct(reload);
  const [open, setOpen] = useState(!!props.formOpen);
  const [brief, setBrief] = useState("");
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [program, setProgram] = useState("");
  const [contract, setContract] = useState(CONTRACT_TEMPLATE);
  const blocker = jointStartBlocker(link);
  const chosen = program || agents[0] || "";

  if (!open)
    return (
      <div className="peer-joint-cta">
        <button type="button" className="test-btn peer-joint-start" disabled={!!blocker} onClick={() => setOpen(true)}>
          {again ? "Start another joint task" : "Start a joint task"}
        </button>
        <span className="set-hint">{blocker || "Build one feature together: your half here, theirs in their repo, one shared contract."}</span>
      </div>
    );

  const ready = !!brief.trim() && (!sandbox || (!!repo.trim() && !!chosen));
  return (
    <form
      className="peer-joint-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready || busy) return;
        void act(
          "propose",
          () =>
            api(jointPath(link.link_id), {
              json: {
                brief: brief.trim(),
                repo_path: repo.trim(),
                branch: branch.trim() || undefined,
                program: chosen || undefined,
                contract_text: contract,
              },
            }),
          "Proposed to " + link.peer_name
        ).then((ok) => ok && setOpen(false));
      }}
    >
      <h4 className="set-subtitle">Start a joint task with {link.peer_name}</h4>
      <label className="peer-field">
        <span className="set-label">What you're building together</span>
        <textarea
          className="peer-joint-brief"
          rows={3}
          value={brief}
          placeholder="e.g. Add GET /items to the API, and make the client fetch and print it"
          onChange={(e) => setBrief(e.target.value)}
        />
      </label>
      {sandbox ? (
        <div className="peer-field">
          <span className="set-label">Your half</span>
          <div className="peer-row">
            <PathField placeholder="your folder" value={repo} onChange={setRepo} pickerTitle="Pick the folder for your half" className="peer-joint-repo" />
            <input placeholder="branch (optional)" value={branch} onChange={(e) => setBranch(e.target.value)} aria-label="Branch" />
            <AgentSelect agents={agents} value={chosen} onChange={setProgram} />
          </div>
          <span className="set-hint">Shared once they accept: an agent works in it inside a sandbox.</span>
        </div>
      ) : (
        <p className="set-hint">Your half needs Linux or macOS — here you can still message them and watch their half.</p>
      )}
      <label className="peer-field">
        <span className="set-label">Starter contract</span>
        <textarea className="peer-joint-contract" rows={9} value={contract} onChange={(e) => setContract(e.target.value)} />
        <span className="set-hint">Both agents read it first; either side can change it later.</span>
      </label>
      <div className="peer-row">
        <button type="submit" className="test-btn peer-joint-propose" disabled={!!busy || !ready}>
          {busy === "propose" ? "Proposing…" : "Propose to " + link.peer_name}
        </button>
        <button type="button" className="test-btn" disabled={!!busy} onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
      {err && <p className="error">{err}</p>}
    </form>
  );
}

function JointWaiting(props: JointProps & { joint: PeerJoint }) {
  const { link, joint, reload } = props;
  const { busy, err, act } = useAct(reload);
  return (
    <div className="peer-joint-waiting">
      <p>
        <strong>Proposed to {link.peer_name}</strong> — waiting for them to accept.
      </p>
      <p className="peer-joint-brief-text">{joint.brief}</p>
      <div className="peer-row">
        <button
          type="button"
          className="test-btn peer-joint-withdraw"
          disabled={!!busy}
          onClick={() => act("cancel", () => api(jointPath(link.link_id, "/cancel"), { json: { reason: "withdrawn" } }), "Proposal withdrawn")}
        >
          {busy === "cancel" ? "Withdrawing…" : "Withdraw"}
        </button>
        {!link.connected && <span className="set-hint">{link.peer_name} is offline — they'll see it when they're back.</span>}
      </div>
      {err && <p className="error">{err}</p>}
    </div>
  );
}

export function JointProposed(props: JointProps & { joint: PeerJoint; formOpen?: boolean }) {
  const { link, joint, agents, sandbox, reload } = props;
  const { busy, err, act } = useAct(reload);
  const [accepting, setAccepting] = useState(!!props.formOpen);
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [program, setProgram] = useState("");
  const chosen = program || agents[0] || "";
  const theirs = joint.theirs || { repo_name: "", branch: "", program: "", shared: false };
  return (
    <div className="peer-joint-proposal">
      <p>
        <strong>{link.peer_name} proposes a joint task</strong>
        {theirs.repo_name ? <span className="muted"> · their half in {theirs.repo_name}</span> : null}
      </p>
      <p className="peer-joint-brief-text">{joint.brief}</p>
      <details className="peer-joint-preview" open>
        <summary>Their starter contract</summary>
        <ContractMarkdown text={joint.contract?.text || ""} />
      </details>
      {accepting ? (
        <div className="peer-joint-accept-form">
          {sandbox ? (
            <>
              <div className="peer-row">
                <PathField placeholder="your folder" value={repo} onChange={setRepo} pickerTitle="Pick the folder for your half" className="peer-joint-repo" />
                <input placeholder="branch (optional)" value={branch} onChange={(e) => setBranch(e.target.value)} aria-label="Branch" />
                <AgentSelect agents={agents} value={chosen} onChange={setProgram} />
              </div>
              <span className="set-hint">Your half starts in this folder as soon as you accept.</span>
            </>
          ) : (
            <span className="set-hint">Your half needs Linux or macOS — accept to message them and watch their half.</span>
          )}
          <div className="peer-row">
            <button
              type="button"
              className="test-btn peer-joint-accept"
              disabled={!!busy || (sandbox && (!repo.trim() || !chosen)) || !link.connected}
              onClick={() =>
                act(
                  "accept",
                  () =>
                    api(jointPath(link.link_id, "/accept"), {
                      json: { repo_path: repo.trim(), branch: branch.trim() || undefined, program: chosen || undefined },
                    }),
                  sandbox ? "Accepted — your half's session is starting" : "Accepted"
                )
              }
            >
              {busy === "accept" ? "Accepting…" : "Accept"}
            </button>
            <button type="button" className="test-btn" disabled={!!busy} onClick={() => setAccepting(false)}>
              Back
            </button>
          </div>
        </div>
      ) : (
        <div className="peer-row">
          <button type="button" className="test-btn peer-joint-accept-with" disabled={!!busy || !link.connected} onClick={() => setAccepting(true)}>
            Accept with…
          </button>
          <button
            type="button"
            className="test-btn peer-joint-decline"
            disabled={!!busy}
            onClick={() => act("decline", () => api(jointPath(link.link_id, "/cancel"), { json: { reason: "declined" } }), "Declined")}
          >
            {busy === "decline" ? "Declining…" : "Decline"}
          </button>
          {!link.connected && <span className="set-hint">{link.peer_name} is offline — answer once they're back.</span>}
        </div>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}

function JointActive(props: JointProps & { joint: PeerJoint; now?: number }) {
  const { link, joint, reload, now } = props;
  const { busy, err, act } = useAct(reload);
  const [confirm, setConfirm] = useState(false);
  const mine = joint.mine || { repo_path: "", branch: "", program: "", share_id: "", session_title: "" };
  const theirs = joint.theirs || { repo_name: "", branch: "", program: "", shared: false };
  const watchable = link.connected && (theirs.shared || !!link.their_share?.shared);

  const openSession = () => {
    if (!mine.session_title) return;
    selectSession(mine.session_title);
    useUi.getState().closeDialog();
  };

  return (
    <div className="peer-joint-active">
      <p className="peer-joint-title">
        <strong>Joint task with {link.peer_name}</strong>
      </p>
      <p className="peer-joint-brief-text">{joint.brief}</p>

      <div className="peer-halves">
        <div className="peer-half peer-half-mine">
          <span className="set-label">Your half</span>
          <span className="set-hint">
            {mine.repo_path ? baseName(mine.repo_path) + (mine.branch ? " · " + mine.branch : "") : "No folder from your side"}
            {mine.program ? " · " + mine.program : ""}
          </span>
          {mine.session_title ? (
            <button type="button" className="test-btn peer-half-open" onClick={openSession}>
              Open session
            </button>
          ) : mine.repo_path ? (
            <span className="set-hint">Its session is starting…</span>
          ) : null}
        </div>
        <div className="peer-half peer-half-theirs">
          <span className="set-label">{link.peer_name}'s half</span>
          <span className="set-hint">
            {theirs.repo_name || "their repo"}
            {theirs.branch ? " · " + theirs.branch : ""}
            {theirs.program ? " · " + theirs.program : ""}
          </span>
          <button
            type="button"
            className="test-btn peer-half-watch"
            disabled={!watchable}
            title={!link.connected ? "They're offline" : watchable ? "Their screen, files and changes" : "Their half hasn't started yet"}
            onClick={() => useUi.getState().openDialogFor("peer-session", link.link_id)}
          >
            Watch their session
          </button>
        </div>
      </div>

      <ContractPanel linkId={link.link_id} peerName={link.peer_name} joint={joint} reload={reload} now={now} />
      <MirrorRow link={link} reload={reload} now={now} />
      <JointCheck check={joint.check} peerName={link.peer_name} />

      <div className="peer-row peer-joint-end">
        <button
          type="button"
          className="test-btn peer-joint-done"
          disabled={!!busy}
          onClick={() => act("done", () => api(jointPath(link.link_id, "/cancel"), { json: { reason: "done" } }), "Joint task marked done")}
        >
          {busy === "done" ? "Finishing…" : "Done"}
        </button>
        <button type="button" className="test-btn peer-joint-cancel" disabled={!!busy} onClick={() => setConfirm(true)}>
          Cancel…
        </button>
      </div>
      {confirm && (
        <InlineConfirm
          title="Cancel the joint task?"
          body={<span>Both sessions keep running — stop sharing separately.</span>}
          confirmLabel="Cancel the task"
          busy={!!busy}
          onCancel={() => setConfirm(false)}
          onConfirm={() => {
            setConfirm(false);
            void act("cancel", () => api(jointPath(link.link_id, "/cancel"), { json: {} }), "Joint task cancelled");
          }}
        />
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
