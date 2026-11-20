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

import { useState } from "react";
import { api } from "../api/client";
import { copyText } from "../lib/clipboard";
import { toast } from "../lib/toast";
import { useDoctor } from "../state/queries";

/** The command that puts the moved-aside file back as state.json, in the
 * same folder: `mv '<backup>' '<dir>/state.json'` — or, for a Windows path,
 * PowerShell's `Move-Item -Force` (the fresh state.json is there already).
 * Both paths single-quoted, so spaces and `$` stay literal. Exported for
 * tests. */
export function restoreCommand(backup: string): string {
  const win = /^[A-Za-z]:[\\/]/.test(backup) || (backup.includes("\\") && !backup.includes("/"));
  const cut = Math.max(backup.lastIndexOf("/"), win ? backup.lastIndexOf("\\") : -1);
  const dest = (cut >= 0 ? backup.slice(0, cut + 1) : "") + "state.json";
  if (win) {
    const q = (x: string) => "'" + x.replace(/'/g, "''") + "'";
    return "Move-Item -Force " + q(backup) + " " + q(dest);
  }
  const q = (x: string) => "'" + x.replace(/'/g, "'\\''") + "'";
  return "mv " + q(backup) + " " + q(dest);
}

export function StateNotice() {
  const notice = useDoctor().data?.state_notice;
  const [dismissed, setDismissed] = useState(false);

  if (!notice || dismissed) return null;

  const dismiss = () => {
    setDismissed(true);
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
            them.{" "}
            <button
              type="button"
              className="sn-copy"
              title={restoreCommand(notice.backup_path)}
              onClick={() =>
                void copyText(restoreCommand(notice.backup_path!)).then((ok) =>
                  toast(ok ? "Copied — run it in a terminal after upgrading" : "Copy failed"),
                )
              }
            >
              Copy the rename command
            </button>
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
