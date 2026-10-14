/** Customize → Pane header: which tabs and buttons a session pane's header
 * shows, and in what order (lib/paneHeader; the synced pref `pane_header`).
 *
 * On top, a PREVIEW: a static header built from the same classes the real
 * one wears (grid/Pane.css), drawn twice — with words, as a roomy pane shows
 * it, and with icons, as a pane too narrow for the words does — so a switch
 * or a move shows its effect at once. Below, two lists of Customize rows,
 * Tabs and Buttons, each ordered among its own kind. ⋯ is not a row: it is
 * always the last thing in the header, and holds whatever is switched off
 * here. */

import type { ReactNode } from "react";
import { useUi } from "../../state/store";
import {
  movePaneItem,
  paneItemDef,
  placePaneItem,
  resolvePaneHeader,
  togglePaneItem,
  type PaneItemId,
  type PaneTabId,
} from "../../lib/paneHeader";
import { TabIcon } from "../grid/TabIcon";
import { InfoTip } from "../InfoTip";
import { CustomizeRow } from "./CustomizeRow";

const ICON = {
  width: 12,
  height: 12,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** One button as the header draws it, inert. */
function previewButton(id: PaneItemId): ReactNode {
  switch (id) {
    case "next":
      return (
        <span key={id} className="nextstep">
          Commit…
        </span>
      );
    case "fasttrack":
      return (
        <span key={id} className="nextstep nextstep-fast">
          ⏩
        </span>
      );
    case "copy":
      return (
        <span key={id} className="act copy-history">
          <svg className="btn-icon" {...ICON}>
            <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
            <rect x="8" y="2" width="8" height="4" rx="1" />
          </svg>
          <span className="btn-label">Copy all</span>
        </span>
      );
    case "history":
      return (
        <span key={id} className="act pane-hist">
          <svg className="btn-icon" {...ICON}>
            <path d="M12 8v4l2 2" />
            <path d="M3.05 11a9 9 0 1 1 .5 4" />
            <path d="M3 22v-6h6" />
          </svg>
          <span className="btn-label">History</span>
        </span>
      );
    case "cost":
      return (
        <span key={id} className="tok usage-trigger">
          <span className="usage-head">
            <span className="uh-cost">~$0.62</span>
          </span>
        </span>
      );
    case "diffstat":
      return (
        <span key={id} className="ctxline">
          <span className="ctx-add">+12</span>
          <span className="ctx-del">−3</span>
          <span className="ctx-files">· 2 files</span>
        </span>
      );
    case "hide":
      return (
        <span key={id} className="act pane-close">
          −
        </span>
      );
    default:
      return null;
  }
}

function PreviewHead({ tabs, buttons, icons }: { tabs: PaneItemId[]; buttons: PaneItemId[]; icons: boolean }) {
  return (
    <div className={"pane-head pane-head-fit cz-preview-head" + (icons ? " head-icons" : "")} aria-hidden="true">
      <span className="grip">⠿</span>
      <span className="title">fix-login-redirect</span>
      <div className="tabs">
        {tabs.map((id) => (
          <button key={id} type="button" tabIndex={-1} className={id === "agent" ? "active" : ""} data-tab={id}>
            <TabIcon name={id as PaneTabId} />
            <span className="tab-label">{paneItemDef(id).label}</span>
          </button>
        ))}
      </div>
      <span className="head-spacer" />
      <div className="actions">{buttons.map(previewButton)}</div>
      <div className="head-tail">
        <span className="act pane-more">⋯</span>
      </div>
    </div>
  );
}

export function PaneHeaderPicker() {
  const pref = useUi((s) => s.paneHeader);
  const setPaneHeader = useUi((s) => s.setPaneHeader);
  const r = resolvePaneHeader(pref);
  const shownTabs = r.tabs.filter((id) => r.shown.has(id));
  const shownButtons = r.buttons.filter((id) => r.shown.has(id));

  const rows = (ids: PaneItemId[], list: string) =>
    ids.map((id, i) => {
      const d = paneItemDef(id);
      return (
        <CustomizeRow
          key={id}
          id={id}
          list={list}
          label={d.label}
          note={d.note}
          on={r.shown.has(id)}
          locked={d.locked}
          lockedWhy="Every header shows the agent"
          onToggle={() => setPaneHeader(togglePaneItem(pref, id))}
          onMove={(dir) => setPaneHeader(movePaneItem(pref, id, dir))}
          onDropBefore={(dragged) => setPaneHeader(placePaneItem(pref, dragged as PaneItemId, id))}
          first={i === 0}
          last={i === ids.length - 1}
        />
      );
    });

  return (
    <div id="customize-pane" className="cz-page">
      <div className="cz-preview" aria-label="Preview of a pane header with your choice" role="img">
        <div className="cz-preview-row">
          <span className="cz-preview-tag">Room to spare</span>
          <PreviewHead tabs={shownTabs} buttons={shownButtons} icons={false} />
        </div>
        <div className="cz-preview-row cz-preview-narrow">
          <span className="cz-preview-tag">Too narrow for words</span>
          <PreviewHead tabs={shownTabs} buttons={shownButtons} icons={true} />
        </div>
      </div>
      <h3 className="cz-heading">
        Tabs
        <InfoTip label="About hidden tabs">
          A hidden tab still shows while you are on it — opened from ⋯, a shortcut or a link — and ⋯ always
          lists it. Terminal never shows for a shared folder, Diff and Map only for a session in a git
          repository, Thread only once a session has workers or messages.
        </InfoTip>
      </h3>
      <ul className="cz-list" aria-label="Pane header tabs">
        {rows(r.tabs, "tabs")}
      </ul>
      <h3 className="cz-heading">
        Buttons
        <InfoTip label="About hidden buttons">
          Everything you switch off here is still one click away in the header's ⋯ menu: the next step
          (Commit…, Push, Open PR), Fast-track…, Browse history, Copy entire history and Hide window. The
          cost and the diff stats are read out at the top of ⋯. During the calm first hour an untouched
          header also leaves Queue and ⏩ to ⋯; once you change anything here it shows exactly your choice.
        </InfoTip>
      </h3>
      <ul className="cz-list" aria-label="Pane header buttons">
        {rows(r.buttons, "buttons")}
      </ul>
      <p className="cz-foot">⋯ is always last: it holds everything switched off here.</p>
    </div>
  );
}
