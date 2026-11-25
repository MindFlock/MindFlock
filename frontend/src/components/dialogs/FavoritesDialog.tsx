/** Favorites (backend core/favorites.py): the sessions you starred because
 * you keep coming back to the same kind of work. Opened from the sidebar's
 * Favorites link, beside Recently closed. An open favorite is one click away
 * (Open); a closed one reopens from its Recently closed entry with the same
 * worktree, branch and conversation (Reopen); one that is gone can only be
 * unstarred. Star or unstar a session from its ⋯ menu. */

import { useQuery } from "@tanstack/react-query";
import type { Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances, refreshRecentlyClosed } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { errorPop } from "../../lib/errorPop";
import { toast } from "../../lib/toast";
import {
  FAVORITES_KEY,
  favoriteStateText,
  refreshFavorites,
  setFavorite,
  sortFavorites,
  type FavoriteRow,
} from "../../lib/favorites";
import { Modal } from "../Modal";

export function FavoritesDialog() {
  const open = useUi((s) => s.openDialog === "favorites");
  const closeDialog = useUi((s) => s.closeDialog);
  const { data, error, isLoading } = useQuery({
    queryKey: FAVORITES_KEY,
    queryFn: () => api<FavoriteRow[]>("/api/favorites"),
    enabled: open,
    staleTime: 5_000,
    retry: false,
  });
  if (!open) return null;
  const rows = sortFavorites(Array.isArray(data) ? data : []);

  const go = (title: string) => {
    closeDialog();
    selectSession(title);
  };

  const reopen = async (r: FavoriteRow) => {
    try {
      const inst = await api<Instance>(`/api/recently-closed/${encodeURIComponent(r.entry_id || "")}/reopen`, {
        method: "POST",
      });
      closeDialog();
      await refreshInstances();
      refreshRecentlyClosed();
      void refreshFavorites();
      if (inst?.title) selectSession(inst.title);
      toast("Reopened " + displayName(inst?.title || r.title));
    } catch (err) {
      errorPop("Reopen failed", (err as Error).message);
      void refreshFavorites();
    }
  };

  return (
    <Modal id="favorites-dialog" onClose={closeDialog}>
      <div id="favorites-panel">
        <div className="ws-head">
          <h2>Favorites</h2>
          <span className="muted" id="favorites-total">
            {rows.length} session{rows.length === 1 ? "" : "s"}
          </span>
          <button type="button" id="favorites-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <p className="set-hint" style={{ margin: 0 }}>
          Sessions you come back to. Star one from its ⋯ menu. A closed favorite reopens with the same folder,
          branch and conversation, and a favorite never closes by itself.
        </p>
        {isLoading && <p className="muted">Loading…</p>}
        {error && <p className="error">{(error as Error).message}</p>}
        {!isLoading && !error && !rows.length && (
          <p className="muted" id="favorites-empty">
            No favorites yet. Open a session's ⋯ menu and choose “Add to favorites”.
          </p>
        )}
        <ul id="favorites-list" className="favorites-list">
          {rows.map((r) => (
            <li key={r.title} className={"fav-row fav-" + r.state} data-title={r.title}>
              <span className="fav-star" aria-hidden="true">
                ★
              </span>
              <span className="fav-name" title={r.folder || r.title}>
                {displayName(r.title)}
              </span>
              <span className="fav-state muted">
                {favoriteStateText(r)}
                {r.branch ? " · " + r.branch : ""}
              </span>
              <span className="fav-acts">
                {r.state === "open" && (
                  <button type="button" className="fav-open" onClick={() => go(r.title)}>
                    Open
                  </button>
                )}
                {r.state === "closed" && (
                  <button
                    type="button"
                    className="fav-reopen"
                    title="Reopen it with the same folder, branch and conversation"
                    onClick={() => void reopen(r)}
                  >
                    Reopen
                  </button>
                )}
                <button
                  type="button"
                  className="fav-unstar"
                  title="Remove from Favorites (the session itself is not touched)"
                  onClick={() => void setFavorite(r.title, false)}
                >
                  Unstar
                </button>
              </span>
            </li>
          ))}
        </ul>
      </div>
    </Modal>
  );
}
