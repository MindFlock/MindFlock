/** Intake → Epics (docs/epics.md): every not-done epic of yours on each
 * Shortcut source, grouped by source and then by workflow state — the epic
 * twin of the Tickets tab's Assigned tickets panel, built from the same kit.
 *
 * An epic starts as ONE lead session that runs a worker per story (side by
 * side where independent, stacked where one blocks another) and opens a PR
 * per story. Rows in a source's auto-start states start by themselves while
 * that source's epic auto-start is on; **Start** starts any other now. Which
 * states auto-start, and the switch, live on the source's card in Tickets —
 * each heading's note is a door there. */

import { useState } from "react";
import { ApiError, api } from "../../api/client";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import { useEpicsQuery } from "../../state/queries";
import { WorkGroup, WorkListPanel, openSessionFromIntake, panelNote, useToggleSet } from "./kit";
import { autoStarts, epicMeta, epicSourceNote, groupEpics, type EpicRow } from "./epics";
import type { TabProps } from "./IntakeDialog";
import "./EpicsTab.css";

export { epicMeta, type EpicRow } from "./epics";

/** Per-device: which source groups / `source::state` groups are COLLAPSED
 * (membership = collapsed, so everything starts open — epics are few). */
const SOURCES_CLOSED_LS_KEY = "mf_intake_epic_sources";
const STATES_CLOSED_LS_KEY = "mf_intake_epic_states";

function EpicItem({ e, busy, onStart }: { e: EpicRow; busy: boolean; onStart(e: EpicRow): void }) {
  const startable = e.startable ?? e.eligible;
  const labels = (e.labels || []).filter(Boolean);
  return (
    <div className="pr-open-item ik-epic" data-epic={e.source + ":" + e.id}>
      <div className="pr-open-main">
        <a
          href={e.url || "#"}
          target="_blank"
          rel="noopener noreferrer"
          className="pr-open-title ik-epic-name"
          title={"Open the epic in " + (e.source_label || "the tracker")}
        >
          {e.name}
        </a>
      </div>
      <div className="pr-open-meta">
        <span>{epicMeta(e)}</span>
        {labels.length ? <span className="ik-epic-labels">{"labels: " + labels.join(", ")}</span> : null}
        {e.has_session ? (
          <span className="pr-open-chip on">lead running</span>
        ) : autoStarts(e) ? (
          <span className="pr-open-chip ok ik-epic-auto" title="In an auto-start state with auto-start on: its lead starts by itself">
            auto-starts
          </span>
        ) : !e.eligible ? (
          (e.reasons || []).map((r) => (
            <span className="pr-open-chip" key={r} title={r}>
              {r}
            </span>
          ))
        ) : null}
      </div>
      <div className="ik-item-start">
        {e.has_session ? (
          <button
            type="button"
            className="btn-primary pr-review-btn ik-open-session"
            title="Close Intake and go to the epic's lead session"
            onClick={() => openSessionFromIntake(e.title)}
          >
            Open lead ›
          </button>
        ) : (
          <button
            type="button"
            className="btn-primary pr-review-btn"
            disabled={!startable || busy}
            title={startable ? "Start the epic's lead session now" : (e.reasons || []).join(" · ") || "Can't start"}
            onClick={() => onStart(e)}
          >
            {busy ? "Starting…" : "Start"}
          </button>
        )}
      </div>
    </div>
  );
}

export function EpicsTab({ gotoTab }: TabProps) {
  const q = useEpicsQuery();
  // The epics starting now, by source:id — several may be in flight at once.
  const [busy, setBusy] = useState<ReadonlySet<string>>(() => new Set());
  const epicKey = (e: EpicRow) => e.source + ":" + e.id;
  const openSources = useToggleSet(SOURCES_CLOSED_LS_KEY, true);
  const openStates = useToggleSet(STATES_CLOSED_LS_KEY, true);
  const groups = groupEpics(q.data);
  const loaded = !!q.data;
  const error = q.error ? "Could not list epics: " + (q.error.message || "error") : "";
  const note = panelNote({ error, fetching: q.isFetching, loaded });

  const start = async (e: EpicRow) => {
    const k = epicKey(e);
    setBusy((b) => new Set(b).add(k));
    try {
      const r = await api<{ title?: string; stories?: number }>("/api/epics/start", { json: { source: e.source, id: e.id } });
      toast("Started " + (r.title || e.title) + " — its lead will run " + (r.stories ?? "your") + " stor" + (r.stories === 1 ? "y" : "ies"));
      void q.refresh();
    } catch (err) {
      errorPop("Couldn't start the epic", err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy((b) => {
        const next = new Set(b);
        next.delete(k);
        return next;
      });
    }
  };

  const toTickets = (
    <button type="button" className="linklike" id="ep-goto-tickets" onClick={() => gotoTab("tickets")}>
      Tickets
    </button>
  );

  return (
    <WorkListPanel
      label="Assigned epics"
      onRefresh={() => void q.refresh()}
      note={note}
      rowId="ep-assigned-row"
      refreshId="ep-epics-refresh"
      noteId="ep-epics-note"
      listId="ep-epics-list"
      hint={
        <>
          One lead agent per epic — it runs a worker and opens a PR per story of yours. Rows marked{" "}
          <strong>auto-starts</strong> start by themselves; <strong>Start</strong> starts any other now.
          Each source's heading says which states auto-start — press it to change them.
        </>
      }
    >
      {error ? <div className="repo-empty">{error}</div> : null}
      {loaded && !groups.length ? (
        <div className="repo-empty" id="ep-no-sources">
          Epics come from a Shortcut source — add one on the {toTickets} tab, then pick its epic states on the source card.
        </div>
      ) : null}
      {groups.map((g) => {
        const sn = epicSourceNote(g.meta);
        return (
          <WorkGroup
            key={g.source}
            groupKey={g.source}
            heading
            name={g.label}
            count={g.total}
            detail={g.error ? "can't list epics" : sn?.text}
            detailTone={g.error ? "bad" : sn?.kind === "nostates" ? "warn" : undefined}
            onDetail={() => gotoTab("tickets", g.source)}
            detailTitle={"Change which epics " + g.label + " auto-starts"}
            open={openSources.isOpen(g.source)}
            onToggle={() => openSources.toggle(g.source)}
          >
            {g.error ? (
              <div className="repo-empty ik-source-error">
                {g.label + ": " + g.error}
              </div>
            ) : null}
            {!g.total && !g.error ? <div className="repo-empty">No open epics of yours.</div> : null}
            {g.states.map((s) => {
              const key = g.source + "::" + s.state;
              return (
                <WorkGroup
                  key={key}
                  indent
                  name={s.state}
                  count={s.rows.length}
                  open={openStates.isOpen(key)}
                  onToggle={() => openStates.toggle(key)}
                >
                  {s.rows.map((e) => (
                    <EpicItem key={e.source + ":" + e.id} e={e} busy={busy.has(epicKey(e))} onStart={(x) => void start(x)} />
                  ))}
                </WorkGroup>
              );
            })}
          </WorkGroup>
        );
      })}
    </WorkListPanel>
  );
}
