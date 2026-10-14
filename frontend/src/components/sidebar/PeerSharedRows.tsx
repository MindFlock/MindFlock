/** "Shared with you" — one rail row per person whose shared session you can
 * watch (a connected peer link whose other end reports a share). Clicking it
 * opens PeerSessionViewer. Your OWN shared sessions are ordinary rows below
 * (with their "Shared with …" chip); these are the ones on THEIR machine. */

import { usePeerLinks } from "../../state/queries";
import { useUi } from "../../state/store";

export function PeerSharedRows() {
  const links = usePeerLinks().data?.links || [];
  const openDialogFor = useUi((s) => s.openDialogFor);
  const theirs = links.filter((l) => l.connected && l.their_share?.shared);
  if (!theirs.length) return null;
  return (
    <div id="peer-shared-rows" role="group" aria-label="Shared with you">
      <div className="psr-head">Shared with you</div>
      {theirs.map((l) => {
        const running = l.their_share?.agent === "running";
        return (
          <button
            key={l.link_id}
            type="button"
            className="psr-row"
            title={"Watch " + l.peer_name + "’s shared session — their screen, files and changes"}
            onClick={() => openDialogFor("peer-session", l.link_id)}
          >
            <span className={"psr-dot" + (running ? " running" : "")} aria-hidden="true" />
            <span>{l.peer_name}’s session</span>
            <span className="psr-sub">{running ? "running" : "stopped"}</span>
          </button>
        );
      })}
    </div>
  );
}
