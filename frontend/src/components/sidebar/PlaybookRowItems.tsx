/** The sidebar row's › menu group for Ship & split: the same items as the
 * pane's fork-icon menu (lib/laneActions.shipMenuModel), acting the same way
 * (runShipEntry) — "When it's done: Open a PR ›" unfolds the lanes in place
 * (the row menu is an inline list, not a popover), then Split into parallel
 * pieces…, Ship it now, Move out of the group for a member, and Message….
 * Rendered for every local session, between the git items and Rename.
 *
 * Nothing here pastes into the agent: each item is a server call. */

import { useMemo, useState } from "react";
import type { Instance } from "../../api/types";
import { refreshInstances, useConfig, useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import {
  LANE_LABEL,
  entryWhy,
  runShipEntry,
  shipMenuModel,
  type ShipEntry,
} from "../../lib/laneActions";
import { isRemote } from "../../lib/playbooks";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";

export function PlaybookRowItems({ inst }: { inst: Instance }) {
  const title = inst.title;
  const { data: config } = useConfig();
  const { data: rows } = useInstances();
  const [laneOpen, setLaneOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const model = useMemo(
    () => shipMenuModel(inst, rows || [], config?.caps),
    [inst, rows, config?.caps]
  );

  // Another device's session gets none: its group and split routes aren't
  // forwarded. A row still being created has nothing to arm yet.
  if (isRemote(inst) || inst.pending) return null;
  const name = displayName(title);

  const run = (e: ShipEntry) => {
    const why = entryWhy(e);
    if (why) {
      toast(why, { duration: 5000 });
      return;
    }
    if (e.kind === "message") {
      useUi.getState().threadOpen(title, { composeTo: title });
      return;
    }
    if (busy) return;
    setBusy(true);
    runShipEntry(e, inst, name, model.current)
      .then((said) => {
        if (said) toast(said, { duration: 4500 });
        refreshInstances();
      })
      .catch((err) => toast(`${name}: ${errMsg(err)}`, { duration: 6000 }))
      .finally(() => setBusy(false));
  };

  const button = (e: ShipEntry, label: string, title2: string, extra = "") => {
    const why = entryWhy(e);
    return (
      <button
        key={e.kind + (e.kind === "lane" ? e.lane : "")}
        className={(why ? "pb-row-off " : "") + extra || undefined}
        aria-disabled={why ? true : undefined}
        title={why || title2}
        data-ship={e.kind === "lane" ? "lane-" + e.lane : e.kind}
        onClick={(ev) => {
          ev.stopPropagation();
          run(e);
        }}
      >
        <span>{label}</span>
      </button>
    );
  };

  const cur = model.current;
  return (
    <>
      <button
        aria-expanded={laneOpen}
        title="How far MindFlock carries this session once its agent is done"
        data-ship="lanes"
        onClick={(e) => {
          e.stopPropagation();
          setLaneOpen((v) => !v);
        }}
      >
        <span>
          When it's done: {LANE_LABEL[cur.lane]}
          {cur.askFirst && cur.lane !== "leave" ? ", asks first" : ""}
        </span>
        <span className="kbd">{laneOpen ? "▾" : "›"}</span>
      </button>
      {laneOpen && (
        <div className="pb-row-sub" role="group" aria-label="When it's done">
          {model.lanes.map((e) =>
            e.kind === "lane"
              ? button(e, e.label + (e.current ? " ✓" : ""), e.desc, e.current ? "on" : "")
              : e.kind === "ask"
                ? button(
                    e,
                    "Ask me before it ships" + (e.on ? " ✓" : ""),
                    "Stop at the next step and show it in the Outbox first"
                  )
                : null
          )}
        </div>
      )}
      {model.split.map((e) =>
        button(
          e,
          "Split into parallel pieces…",
          "The agent proposes pieces with separate paths; you approve, MindFlock runs and merges them back"
        )
      )}
      {model.tail.map((e) =>
        e.kind === "shipnow" ? (
          button(
            e,
            "Ship it now",
            "Don't wait for the agent — take what's there through the lane now"
          )
        ) : e.kind === "detach" ? (
          button(
            e,
            "Move out of " + (model.group?.name || "the group"),
            "The group stops driving it; the session and its lane stay"
          )
        ) : e.kind === "message" ? (
          <button
            key="message"
            onClick={(ev) => {
              ev.stopPropagation();
              run(e);
            }}
          >
            Message…<span className="kbd">Ctrl+K S</span>
          </button>
        ) : null
      )}
      <div className="menu-sep" />
    </>
  );
}
