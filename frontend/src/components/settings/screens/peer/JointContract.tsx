/** The Contract panel of an active joint task: the shared CONTRACT.md both
 * agents follow, rendered, with its revision and who changed it last. Edit
 * replaces it (a new revision, synced to the other side); each open proposal
 * — an agent asking to change it — waits here for a person's Accept or
 * Reject. Agents never accept their own changes. */

import { useState } from "react";
import { api } from "../../../../api/client";
import { relTime } from "../../../../lib/format";
import { contractLineDiff, isoSeconds, jointPath, openProposals, syncedText, type PeerJoint } from "../../../../lib/peer";
import { ContractMarkdown } from "./ContractMarkdown";
import { useAct } from "./useAct";

function whoText(by: string, peerName: string, agent = false): string {
  if (by === "peer") return agent ? peerName + "'s agent" : peerName;
  return agent ? "Your agent" : "you";
}

/** A proposal as what it changes (+ added, − removed lines), with the whole
 * proposed contract one click away. */
function ProposalChanges({ before, after }: { before: string; after: string }) {
  const diff = contractLineDiff(before, after);
  return (
    <>
      {diff.length ? (
        <pre className="peer-diff-lines" aria-label="What it changes">
          {diff.map((d, i) => (
            <span key={i} className={d.op === "+" ? "peer-diff-add" : "peer-diff-del"}>
              {(d.op === "+" ? "+ " : "− ") + d.text}
            </span>
          ))}
        </pre>
      ) : (
        <p className="set-hint">It changes nothing but blank lines.</p>
      )}
      <details>
        <summary>The whole proposed contract</summary>
        <ContractMarkdown text={after} empty="(an empty contract)" />
      </details>
    </>
  );
}

export function ContractPanel(props: {
  linkId: string;
  peerName: string;
  joint: Pick<PeerJoint, "contract" | "proposals">;
  reload(): void;
  now?: number;
}) {
  const { linkId, peerName, joint, reload, now } = props;
  const { busy, err, act } = useAct(reload);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const c = joint.contract || { text: "", rev: 0, updated: "", by: "me" };
  const open = openProposals(joint);
  const t = isoSeconds(c.updated);

  return (
    <div className="peer-contract" data-rev={c.rev}>
      <div className="peer-contract-head">
        <span className="set-label">Contract</span>
        <span className="set-hint peer-contract-rev">
          rev {c.rev}
          {Number.isFinite(t) ? " · changed by " + whoText(c.by, peerName) + " " + relTime(t, { now }) : ""}
        </span>
        {!editing && (
          <button
            type="button"
            className="test-btn peer-contract-edit"
            disabled={!!busy}
            onClick={() => {
              setDraft(c.text);
              setEditing(true);
            }}
          >
            Edit
          </button>
        )}
      </div>
      {editing ? (
        <div className="peer-contract-editor">
          <textarea rows={10} value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="Contract" />
          <div className="peer-row">
            <button
              type="button"
              className="test-btn peer-contract-save"
              disabled={!!busy || draft === c.text}
              onClick={async () => {
                const ok = await act(
                  "save",
                  () => api(jointPath(linkId, "/contract"), { method: "PUT", json: { text: draft } }),
                  (r) => syncedText("Contract saved — both agents get the new version", r as { synced?: boolean }, peerName)
                );
                if (ok) setEditing(false);
              }}
            >
              {busy === "save" ? "Saving…" : "Save"}
            </button>
            <button type="button" className="test-btn" disabled={!!busy} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
          <span className="set-hint">Saving sends it to {peerName} and rewrites CONTRACT.md in both folders.</span>
        </div>
      ) : (
        <ContractMarkdown text={c.text} empty="The contract is empty — Edit to write one." />
      )}

      {open.length > 0 && (
        <ul className="peer-proposals">
          {open.map((p) => (
            <li key={p.id} className="peer-proposal" data-proposal-id={p.id}>
              <p className="peer-proposal-who">
                <strong>{whoText(p.by, peerName, true)}</strong> proposes a change
                {Number.isFinite(isoSeconds(p.at)) ? <span className="muted"> · {relTime(isoSeconds(p.at), { now })}</span> : null}
              </p>
              <ProposalChanges before={c.text} after={p.text} />
              <div className="peer-row">
                <button
                  type="button"
                  className="test-btn peer-proposal-accept"
                  disabled={!!busy}
                  onClick={() =>
                    act(
                      "accept:" + p.id,
                      () => api(jointPath(linkId, "/proposals/" + encodeURIComponent(p.id) + "/accept"), { json: {} }),
                      (r) => syncedText("Change accepted — the contract is updated", r as { synced?: boolean }, peerName)
                    )
                  }
                >
                  {busy === "accept:" + p.id ? "Accepting…" : "Accept"}
                </button>
                <button
                  type="button"
                  className="test-btn peer-proposal-reject"
                  disabled={!!busy}
                  onClick={() =>
                    act(
                      "reject:" + p.id,
                      () => api(jointPath(linkId, "/proposals/" + encodeURIComponent(p.id) + "/reject"), { json: {} }),
                      (r) => syncedText("Change rejected", r as { synced?: boolean }, peerName)
                    )
                  }
                >
                  {busy === "reject:" + p.id ? "Rejecting…" : "Reject"}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
