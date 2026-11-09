/** Intake → Tickets → Epics (docs/epics.md): your epics in the states a
 * source ingests ("Epics in these states" on the source card), each with how
 * many of YOUR stories in it are still to do, and a Start button that starts
 * the epic's LEAD session — one agent per epic that runs a worker per story
 * (side by side where independent, stacked where one blocks another) and
 * opens a PR per story. Hidden while no source ingests epics. */

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../../api/client";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import "./EpicsSection.css";

export interface EpicRow {
  source: string;
  source_label: string;
  id: string;
  title: string;
  name: string;
  url: string;
  state: string;
  stories_total: number;
  mine_open: number | null;
  has_session: boolean;
  auto: boolean;
  labels?: string[];
  /** Would start by itself: in the states, carries one of the source's
   * Auto-start labels (when any are set), not already taken. */
  eligible: boolean;
  /** Start works: not taken, stories of yours left (a missing label only
   * stops the AUTO start). Older servers: falls back to `eligible`. */
  startable?: boolean;
  reasons: string[];
}

/** The line under an epic's name: its state, your open stories, auto. */
export function epicMeta(e: Pick<EpicRow, "state" | "mine_open" | "stories_total" | "auto">): string {
  const bits = [e.state];
  if (e.mine_open == null) bits.push(e.stories_total + " stories");
  else bits.push(e.mine_open + " of yours to do" + (e.stories_total ? " (of " + e.stories_total + ")" : ""));
  if (e.auto) bits.push("starts by itself");
  return bits.filter(Boolean).join(" · ");
}

export function EpicsSection() {
  const [rows, setRows] = useState<EpicRow[] | null>(null);
  const [errors, setErrors] = useState<{ source: string; error: string }[]>([]);
  const [busy, setBusy] = useState<string>("");

  const load = useCallback(async (refresh = false) => {
    try {
      const r = await api<{ epics?: EpicRow[]; errors?: { source: string; error: string }[] }>("/api/epics" + (refresh ? "?refresh=1" : ""));
      setRows(Array.isArray(r.epics) ? r.epics : []);
      setErrors(Array.isArray(r.errors) ? r.errors : []);
    } catch {
      setRows([]);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => {
      if (!document.hidden) void load();
    }, 60_000);
    return () => clearInterval(t);
  }, [load]);

  if (!rows || (!rows.length && !errors.length)) return null;

  const start = async (e: EpicRow) => {
    setBusy(e.title);
    try {
      const r = await api<{ title?: string; stories?: number }>("/api/epics/start", { json: { source: e.source, id: e.id } });
      toast("Started " + (r.title || e.title) + " — its lead will run " + (r.stories ?? "your") + " stor" + (r.stories === 1 ? "y" : "ies"));
      void load(true);
    } catch (err) {
      errorPop("Couldn't start the epic", err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="ik-epics" aria-label="Epics">
      <div className="ik-epics-head">
        <h3>Epics</h3>
        <span className="muted">one lead agent per epic · a worker and a PR per story</span>
        <button type="button" className="ik-epics-refresh" title="Ask Shortcut again" onClick={() => void load(true)}>
          ↻
        </button>
      </div>
      {errors.map((x) => (
        <p key={x.source} className="ik-epics-error">
          {x.source}: {x.error}
        </p>
      ))}
      {!rows.length ? (
        <p className="muted ik-epics-empty">No epics of yours in those states.</p>
      ) : (
        <ul className="ik-epics-list">
          {rows.map((e) => (
            <li key={e.source + ":" + e.id} className="ik-epic">
              <div className="ik-epic-main">
                <a className="ik-epic-name" href={e.url} target="_blank" rel="noreferrer">
                  {e.name}
                </a>
                <div className="ik-epic-meta muted">{epicMeta(e)}</div>
                {!e.eligible && !e.has_session && e.reasons.length > 0 && <div className="ik-epic-why muted">{e.reasons.join(" · ")}</div>}
              </div>
              {e.has_session ? (
                <button type="button" onClick={() => void selectSession(e.title)} title="Go to the epic's lead session">
                  Open lead
                </button>
              ) : (
                <button type="button" className="btn-primary" disabled={!(e.startable ?? e.eligible) || busy === e.title} onClick={() => void start(e)}>
                  {busy === e.title ? "Starting…" : "Start"}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
