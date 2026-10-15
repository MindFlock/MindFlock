/** "Mirror their work into [repo] as peer/<name>": the standing form of
 * Export. Whenever their shared folder's HEAD moves, MindFlock fetches it
 * into one of my repos as a `peer/…` branch — never touching that repo's
 * working tree. One row: the form when off, the status line (last update or
 * last error) and Disable when on. */

import { useState } from "react";
import { api } from "../../../../api/client";
import { PathField } from "../../../PathField";
import { defaultMirrorBranch, mirrorStatusText, type PeerLink } from "../../../../lib/peer";
import { useAct } from "./useAct";

export function MirrorRow(props: { link: Pick<PeerLink, "link_id" | "peer_name" | "mirror">; reload(): void; now?: number }) {
  const { link, reload, now } = props;
  const m = link.mirror || null;
  const base = "/api/peer/links/" + encodeURIComponent(link.link_id) + "/mirror";
  const { busy, err, act } = useAct(reload);
  const [repo, setRepo] = useState(m?.repo_path || "");
  const [branch, setBranch] = useState(m?.branch || defaultMirrorBranch(link.peer_name));
  const on = !!m?.enabled;
  const branchOk = branch.startsWith("peer/") && branch.length > 5;

  return (
    <div className="peer-mirror" data-mirror={on ? "on" : "off"}>
      <span className="set-label">Mirror</span>
      {on && m ? (
        <div className="peer-mirror-on">
          <span className="set-hint peer-mirror-status">{mirrorStatusText(m, link.peer_name, now)}</span>
          <button
            type="button"
            className="test-btn peer-mirror-disable"
            disabled={!!busy}
            onClick={() => act("mirror", () => api(base, { method: "DELETE" }), "Mirror off — the branch stays in your repo")}
          >
            {busy === "mirror" ? "Turning off…" : "Disable"}
          </button>
        </div>
      ) : (
        <div className="peer-mirror-form">
          <span className="set-hint">Mirror their work into</span>
          <PathField
            placeholder="your repo"
            value={repo}
            onChange={setRepo}
            pickerTitle="Pick the repo to mirror their work into"
            className="peer-mirror-repo"
          />
          <span className="set-hint">as</span>
          <input className="peer-mirror-branch" aria-label="Branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
          <button
            type="button"
            className="test-btn peer-mirror-enable"
            disabled={!!busy || !repo.trim() || !branchOk}
            onClick={() =>
              act(
                "mirror",
                () => api(base, { json: { repo_path: repo.trim(), branch } }),
                "Mirroring " + link.peer_name + "'s work into " + branch
              )
            }
          >
            {busy === "mirror" ? "Turning on…" : "Enable"}
          </button>
        </div>
      )}
      {!on && !branchOk && <span className="set-hint">The branch name starts with peer/.</span>}
      {m?.last_error && (
        <p className="error peer-mirror-error">Last mirror failed: {m.last_error}</p>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
