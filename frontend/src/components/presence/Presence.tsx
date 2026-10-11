/** Presence on a session (lib/presence.ts): the rail row's dots and the pane
 * header's one drive control. Both render nothing while you are alone on the
 * session. Styles live with their hosts: `.presence` in
 * sidebar/SidebarRow.css, `.drive-ctl` in grid/Pane.css. */

import { useState } from "react";
import type { Instance } from "../../api/types";
import { refreshInstances } from "../../state/queries";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { driveControl, presenceDots, setDriver } from "../../lib/presence";

type PresenceInst = Pick<Instance, "title" | "viewers" | "driver" | "driver_name">;

/** Up to three initials dots — the driver's ringed — plus "+N". */
export function PresenceDots({ inst, me }: { inst: PresenceInst; me: string }) {
  const { dots, more } = presenceDots(inst, me);
  if (!dots.length) return null;
  const tip = dots.map((d) => d.tip).join("\n") + (more ? `\n+${more} more` : "");
  return (
    <span className="presence" title={tip} aria-label={tip.replace(/\n/g, "; ")}>
      {dots.map((d) => (
        <span
          key={d.person}
          className={"presence-dot" + (d.driving ? " driving" : "") + (d.typing ? " typing" : "")}
          title={d.tip}
        >
          {d.initials}
        </span>
      ))}
      {more > 0 && <span className="presence-more">+{more}</span>}
    </span>
  );
}

/** "Driving" (click: let go) / "Watching — take control" (click: take it). */
export function DriveButton({ inst, me }: { inst: PresenceInst; me: string }) {
  const [busy, setBusy] = useState(false);
  const ctl = driveControl(inst, me);
  if (!ctl) return null;
  return (
    <button
      type="button"
      className={"drive-ctl" + (ctl.driving ? " driving" : " watching")}
      title={ctl.tip}
      aria-label={ctl.tip}
      disabled={busy}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={async (e) => {
        e.stopPropagation();
        setBusy(true);
        try {
          await setDriver(inst.title, ctl.action);
          refreshInstances();
        } catch (err) {
          toast("Couldn't " + (ctl.action === "take" ? "take control" : "let go") + ": " + errMsg(err));
        } finally {
          setBusy(false);
        }
      }}
    >
      {ctl.label}
    </button>
  );
}
