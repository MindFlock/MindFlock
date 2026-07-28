/** Downgrade notice — shown when this build refused to read a state.json
 * written by a NEWER MindFlock. LoadState moves that file aside and starts
 * with an empty session list, which from the user's chair is indistinguishable
 * from "MindFlock deleted all my sessions". Nothing is lost, but only a server
 * log said so; this says it where the user is actually looking, and names the
 * file to rename back.
 *
 * Dismissal is server-side (POST /api/doctor/ack-state-notice) rather than
 * local state: the notice describes a one-time event, so once acknowledged it
 * must not return on the next reload. */

import { useEffect, useState } from "react";
import { api } from "../api/client";

type Notice = {
  file_version?: number;
  supported_version?: number;
  backup_path?: string;
};

export function StateNotice() {
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    let live = true;
    api<{ state_notice?: Notice | null }>("/api/doctor")
      .then((d) => {
        if (live && d && d.state_notice) setNotice(d.state_notice);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  if (!notice) return null;

  const dismiss = () => {
    setNotice(null);
    api("/api/doctor/ack-state-notice", { method: "POST" }).catch(() => {});
  };

  return (
    <div id="state-notice" role="alert">
      <div className="sn-head">⚠ Your sessions aren’t gone</div>
      <div className="sn-detail">
        This state file was written by a newer MindFlock (v{notice.file_version} — this
        build reads v{notice.supported_version}), so it started with an empty session list
        rather than rewriting the file and stripping fields it doesn’t understand.
        {notice.backup_path ? (
          <>
            {" "}
            Nothing was deleted. Your sessions are preserved in{" "}
            <code className="sn-path">{notice.backup_path}</code> — upgrade MindFlock, then
            rename that file back to <code className="sn-path">state.json</code> to recover
            them.
          </>
        ) : (
          " The original file could not be moved aside, so check ~/.mindflock/state.json before making changes."
        )}
      </div>
      <div className="sn-foot">
        <button type="button" className="sn-btn" onClick={dismiss}>
          Got it
        </button>
      </div>
    </div>
  );
}
