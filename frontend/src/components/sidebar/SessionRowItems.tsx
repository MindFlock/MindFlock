/** The sidebar row's › menu group for a session's hand-offs, between the git
 * items and Rename:
 *
 *   Fast-track…                  opens the pane's ⏩ picker — the ONE
 *                                fast-track control, never a second copy of
 *                                its choices (Ctrl+K F)
 *   Split into parallel pieces…  a split run with this session as the lead
 *   Move out of <group>          a group MEMBER only: the group stops driving
 *                                it, its session and fast-track stay
 *   Message…                     the Thread composer, as you (Ctrl+K S)
 *
 * Every item is a server call or opens a place you type; nothing is pasted
 * into the agent. */

import { useState } from "react";
import type { Instance } from "../../api/types";
import { refreshInstances, useConfig } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import {
  LANE_SHORT,
  detachableGroup,
  laneChoice,
  moveOutOfGroup,
  openFastTrackMenu,
  splitBlockReason,
  splitSession,
} from "../../lib/laneActions";
import { isRemote } from "../../lib/playbooks";
import { fastTrackStep } from "../../lib/stage";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";

export function SessionRowItems({ inst }: { inst: Instance }) {
  const title = inst.title;
  const { data: config } = useConfig();
  const [busy, setBusy] = useState(false);

  // A row still being created has nothing to set or split yet.
  if (inst.pending) return null;
  const name = displayName(title);
  // Another device's session: its group and split routes aren't forwarded.
  const remote = isRemote(inst);
  const ft = fastTrackStep(inst);
  const cur = laneChoice(inst);
  const splitWhy = remote ? "" : splitBlockReason(config?.caps, inst);
  const group = remote ? null : detachableGroup(inst);

  const act = (why: string, fn: () => Promise<string>) => (ev: React.MouseEvent) => {
    ev.stopPropagation();
    if (why) {
      toast(why, { duration: 5000 });
      return;
    }
    if (busy) return;
    setBusy(true);
    fn()
      .then((said) => {
        if (said) toast(said, { duration: 4500 });
        refreshInstances();
      })
      .catch((err) => toast(`${name}: ${errMsg(err)}`, { duration: 6000 }))
      .finally(() => setBusy(false));
  };

  return (
    <>
      {ft && (
        <button
          data-row="fast-track"
          title={ft.title}
          onClick={(ev) => {
            ev.stopPropagation();
            openFastTrackMenu(title);
          }}
        >
          {/* The short word, as on the ⏩ button: the full sentence (and
              "asks first") is the tooltip — a row menu is narrow. */}
          <span>
            Fast-track… <span className="muted">{LANE_SHORT[cur.lane]}</span>
          </span>
          <span className="kbd">Ctrl+K F</span>
        </button>
      )}
      {!remote && (
        <button
          data-row="split"
          className={splitWhy ? "pb-row-off" : undefined}
          aria-disabled={splitWhy ? true : undefined}
          title={
            splitWhy ||
            "The agent proposes pieces with separate paths; you approve them and pick where they run — separate worktrees merged back, or this folder"
          }
          onClick={act(splitWhy, () => splitSession(inst, name))}
        >
          <span>Split into parallel pieces…</span>
        </button>
      )}
      {group && (
        <button
          data-row="detach"
          title="The group stops driving it; the session and its fast-track stay"
          onClick={act("", async () => {
            await moveOutOfGroup(group.id, group.task);
            return `${name} is out of ${group.name} — its session and fast-track stay as they are`;
          })}
        >
          <span>Move out of {group.name}</span>
        </button>
      )}
      <button
        data-row="message"
        onClick={(ev) => {
          ev.stopPropagation();
          useUi.getState().threadOpen(title, { composeTo: title });
        }}
      >
        Message…<span className="kbd">Ctrl+K S</span>
      </button>
      <div className="menu-sep" />
    </>
  );
}
