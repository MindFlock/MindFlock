/** The sidebar row's › menu group for agent teams: the same playbooks as the
 * pane's fork-icon menu (Split across workers…, Ask a session…, and with
 * workers Check on workers / Wrap up workers), then Message…. Rendered only
 * for a CLI that gets the MindFlock tools, between the git items and Rename.
 *
 * The row menu is an inline list rather than a popover, so "Ask a session…"
 * unfolds its session picker in place instead of opening a submenu. */

import { Fragment, useEffect, useMemo, useState } from "react";
import type { Instance, Playbook } from "../../api/types";
import { useConfig, useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import {
  askTargets,
  fetchPlaybooks,
  forkBlockReason,
  liveChildren,
  mcpCapable,
  menuModel,
  pastePlaybook,
} from "../../lib/playbooks";
import { toast } from "../../lib/toast";

export function PlaybookRowItems({ inst }: { inst: Instance }) {
  const title = inst.title;
  const { data: config } = useConfig();
  const { data: rows } = useInstances();
  const railOrder = useUi((s) => s.railOrder);
  // Another device's session gets none: mcpCapable is false for a remote row.
  const capable = mcpCapable(config?.caps, inst);
  const [list, setList] = useState<Playbook[] | null>(null);
  const [askOpen, setAskOpen] = useState(false);

  useEffect(() => {
    if (!capable) return;
    let live = true;
    fetchPlaybooks(title)
      .then((l) => live && setList(l))
      .catch(() => live && setList([]));
    return () => {
      live = false;
    };
  }, [capable, title]);

  const children = useMemo(() => liveChildren(title, rows || []), [title, rows]);
  const model = useMemo(() => menuModel(list || [], children), [list, children]);
  const targets = useMemo(
    () => (askOpen ? askTargets(title, rows || [], railOrder, displayName) : []),
    [askOpen, title, rows, railOrder]
  );

  if (!capable) return null;
  const blocked = forkBlockReason(inst);
  const askPb = (list || []).find((p) => p.args.some((a) => a.kind === "session" && a.required));

  const button = (pb: Playbook) => {
    const why = blocked || (pb.available ? "" : pb.disabled_reason || "Not available right now");
    const isAsk = pb === askPb;
    return (
      <button
        key={pb.id}
        className={why ? "pb-row-off" : undefined}
        aria-disabled={why ? true : undefined}
        aria-expanded={isAsk ? askOpen : undefined}
        title={why || pb.desc}
        data-playbook={pb.id}
        onClick={(e) => {
          e.stopPropagation();
          if (why) {
            toast(why, { duration: 5000 });
            return;
          }
          if (isAsk) {
            setAskOpen((v) => !v);
            return;
          }
          void pastePlaybook(title, pb);
        }}
      >
        <span>
          {pb.label}
          {pb.id === "wrapup" && model.workers ? ` (${model.workers.reported})` : ""}
        </span>
        {isAsk && <span className="kbd">{askOpen ? "▾" : "›"}</span>}
      </button>
    );
  };

  return (
    <>
      {list === null && (
        <button className="pb-row-off" aria-disabled disabled>
          Work with other sessions…
        </button>
      )}
      {model.general.map((pb) => (
        <Fragment key={pb.id}>
          {button(pb)}
          {pb === askPb && askOpen && (
            <div className="pb-row-sub" role="group" aria-label="Ask which session">
              {targets.length === 0 && <span className="muted pb-row-none">No other sessions</span>}
              {targets.map((t) => (
                <button
                  key={t.title}
                  onClick={(e) => {
                    e.stopPropagation();
                    setAskOpen(false);
                    void pastePlaybook(title, pb, { session: t.title });
                  }}
                >
                  <span>{t.name}</span>
                  {t.rel && <span className="rel">{t.rel}</span>}
                </button>
              ))}
            </div>
          )}
        </Fragment>
      ))}
      {model.workers?.items.map(button)}
      <button
        onClick={(e) => {
          e.stopPropagation();
          useUi.getState().threadOpen(title, { composeTo: title });
        }}
      >
        Message…<span className="kbd">Ctrl+K S</span>
      </button>
      <div className="menu-sep" />
    </>
  );
}
