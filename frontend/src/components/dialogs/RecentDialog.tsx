/** Recently-closed sessions dialog (port of loadRecentlyClosed, section 20):
 * reopen / wipe worktree / forget. */

import { useCallback, useEffect, useState } from "react";
import type { Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";

interface ClosedEntry {
  id: string;
  title?: string;
  /** The branch the session owned. The server has always sent this; the row used
   * to show only `title`, which for an ingested session is the bare slug
   * ("shortcut-21018") and says nothing about what the work was. */
  branch?: string;
  folder?: string;
  closed_at?: string | number;
  exists: boolean;
  in_place?: boolean;
  provisioned?: boolean;
}

function fmtClosedAt(iso: string | number | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleString();
}

export function RecentDialog() {
  const open = useUi((s) => s.openDialog === "recent");
  const closeDialog = useUi((s) => s.closeDialog);
  const [list, setList] = useState<ClosedEntry[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await api<ClosedEntry[]>("/api/recently-closed");
      setError("");
      setList(Array.isArray(data) ? data : []);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  if (!open) return null;
  const data = list || [];

  return (
    <div
      id="recent-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="recent-panel">
        <div className="ws-head">
          <h2>Recently closed</h2>
          <span id="recent-total" className="muted">
            {data.length} session{data.length === 1 ? "" : "s"}
          </span>
          <button type="button" id="recent-refresh" onClick={load}>
            Refresh
          </button>
          <button type="button" id="recent-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div id="recent-list">
          {list === null ? (
            <p className="muted">Loading…</p>
          ) : !data.length ? (
            <p className="muted">No recently closed sessions.</p>
          ) : (
            data.map((e) => {
              const gone = !e.exists;
              return (
                <div className="recent-row" key={e.id}>
                  <div className="recent-info">
                    {/* The BRANCH is the headline: it carries the descriptive tail
                        ("…/path-expansion-never-routes-cloudflare") that says what
                        the session was for. The bare title is the slug, which for an
                        ingested ticket is just its number. Deliberately NOT run
                        through displayBranch() — that strips the prefix, and here the
                        whole name is the point. */}
                    <span
                      className="recent-name"
                      title={[
                        e.branch ? "Branch: " + e.branch : "",
                        e.title ? "Session: " + e.title : "",
                        e.folder || "",
                      ]
                        .filter(Boolean)
                        .join("\n")}
                    >
                      {e.branch || e.title || "(untitled)"}
                    </span>
                    <span className="recent-sub">
                      {/* Keep the session identity visible: it is what Reopen acts on
                          and what the wipe confirmation names. */}
                      {e.branch && e.title && (
                        <span className="recent-slug muted">{e.title}</span>
                      )}
                      {e.in_place && <span className="ws-badge kind">in-place</span>}
                      {e.provisioned && <span className="ws-badge kind">provisioned</span>}
                      {gone && <span className="ws-badge gone">worktree gone</span>}
                      <span className="recent-when muted">{fmtClosedAt(e.closed_at)}</span>
                    </span>
                  </div>
                  <div className="recent-actions">
                    <button
                      className="recent-reopen"
                      disabled={gone}
                      onClick={async () => {
                        try {
                          const inst = await api<Instance>(
                            `/api/recently-closed/${encodeURIComponent(e.id)}/reopen`,
                            { method: "POST" }
                          );
                          closeDialog();
                          await refreshInstances();
                          if (inst?.title) selectSession(inst.title);
                        } catch (err) {
                          alert("Reopen failed: " + (err as Error).message);
                          load();
                        }
                      }}
                    >
                      Reopen
                    </button>
                    {!e.in_place && !gone && (
                      <button
                        className="recent-wipe danger"
                        onClick={async () => {
                          if (
                            !confirm(
                              `Permanently delete the worktree for '${e.title || ""}'?\nThis cannot be undone.`
                            )
                          )
                            return;
                          try {
                            await api(`/api/recently-closed/${encodeURIComponent(e.id)}/forget`, {
                              json: { wipe: true },
                            });
                          } catch (err) {
                            alert("Wipe failed: " + (err as Error).message);
                            return;
                          }
                          load();
                        }}
                      >
                        Wipe worktree
                      </button>
                    )}
                    <button
                      className="recent-forget"
                      onClick={async () => {
                        try {
                          await api(`/api/recently-closed/${encodeURIComponent(e.id)}/forget`, {
                            json: { wipe: false },
                          });
                        } catch (err) {
                          alert("Forget failed: " + (err as Error).message);
                          return;
                        }
                        load();
                      }}
                    >
                      Forget
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
        <p id="recent-error" className="error">{error}</p>
      </div>
    </div>
  );
}
