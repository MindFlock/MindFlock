/** New → Ticket: describe work, get a ticket filed on a real source.
 *
 * The other half of the New dialog, and deliberately not a ticket form. There
 * is one input — a sentence or two about the work — and one button. What comes
 * back is a link to a ticket that now exists in the tracker, which is the only
 * thing MindFlock can give you here that the tracker cannot give you faster:
 * every tracker already has a create form, and a worse copy of one living
 * inside a session dialog would earn nothing.
 *
 * So there is no title field, no description field and no way to hand-write a
 * ticket. The backend matches — ``POST /api/tickets/compose`` takes a source
 * and a sentence, and no route anywhere accepts ticket fields.
 *
 * The wait is the interesting design problem: filing runs a model turn (~10-25s)
 * and then a write to somebody else's API, so the button is held for that whole
 * time, the label admits it once the turn runs long, and a failure hands back
 * the draft it already paid for rather than making the retry cost the wait
 * again.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import { errMsg } from "../../lib/format";
import { useUi } from "../../state/store";

/** One configured ticketing source, and whether it will accept a ticket. */
interface TicketSource {
  key: string;
  label: string;
  provider: string;
  can_create: boolean;
  /** Why not — a sentence naming the field to set. Empty when it can. */
  blocker: string;
}

interface SourcesPayload {
  sources?: TicketSource[];
  /** Whether ticket ingestion is on for the flock, i.e. whether filing this
   * ticket will also, eventually, start a session for it. */
  ingest_on?: boolean;
}

/** A ticket that now exists. `url` is the whole point of the feature. */
interface FiledTicket {
  source: string;
  source_label: string;
  provider: string;
  id: string;
  slug: string;
  name: string;
  url: string;
  description: string;
  criteria?: string[];
}

/** What the model wrote, handed back when the FILING failed rather than the
 * drafting. Shown so a retry is one button rather than another model turn. */
interface Draft {
  name: string;
  description: string;
  criteria?: string[];
}

/** Below this a sentence names no work worth filing. Matches the server's
 * `ticket_draft.MIN_SENTENCE`, and exists here only to keep the button quiet
 * until there is something to press it about — the server enforces it. */
const MIN_BRIEF = 6;

/** What the box will send, matching the server's own MAX_SENTENCE. A pasted
 * paragraph is truncated rather than refused: a brief is still a request. */
const MAX_BRIEF = 2000;

/** How long the filing runs before the label admits it. Same number the
 * Describe box uses, for the same reason: a model turn plus a cold CLI start is
 * ~10-25s, and a button that has said "Filing…" without changing for half a
 * minute reads as a hang long before it actually is one. */
const SLOW_MS = 8000;

export function NewTicketPane() {
  const closeDialog = useUi((s) => s.closeDialog);
  const openDialogFor = useUi((s) => s.openDialogFor);

  const [sources, setSources] = useState<TicketSource[] | null>(null);
  const [ingestOn, setIngestOn] = useState(false);
  const [sourcesError, setSourcesError] = useState("");
  const [source, setSource] = useState("");
  const [brief, setBrief] = useState("");
  const [filing, setFiling] = useState(false);
  const [slow, setSlow] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [filed, setFiled] = useState<FiledTicket | null>(null);
  const briefRef = useRef<HTMLTextAreaElement | null>(null);
  const slowTimer = useRef<number | null>(null);

  useEffect(() => {
    let live = true;
    api<SourcesPayload>("/api/tickets/sources")
      .then((p) => {
        if (!live) return;
        const rows = p.sources || [];
        setSources(rows);
        setIngestOn(!!p.ingest_on);
        // Pre-pick when there is exactly one source that would accept a
        // ticket. Not "the first usable one" — with two connected trackers,
        // which one a ticket lands in is a decision, and a silent default is
        // how work gets filed on the wrong board.
        const usable = rows.filter((r) => r.can_create);
        if (usable.length === 1) setSource(usable[0].key);
      })
      .catch((err) => live && setSourcesError(errMsg(err)));
    return () => {
      live = false;
    };
  }, []);

  // The box is the only thing on this pane, so it takes focus on arrival —
  // switching to this tab and typing has to work without a click.
  useEffect(() => {
    briefRef.current?.focus();
  }, []);

  useEffect(
    () => () => {
      if (slowTimer.current) window.clearTimeout(slowTimer.current);
    },
    []
  );

  const chosen = (sources || []).find((s) => s.key === source) || null;
  const ready = brief.trim().length >= MIN_BRIEF && !!chosen?.can_create && !filing;

  const file = useCallback(async () => {
    if (!ready) return;
    setError("");
    setDraft(null);
    setFiling(true);
    setSlow(false);
    slowTimer.current = window.setTimeout(() => setSlow(true), SLOW_MS);
    try {
      const row = await api<FiledTicket>("/api/tickets/compose", {
        json: { source, text: brief.trim() },
      });
      setFiled(row);
    } catch (err) {
      setError(errMsg(err));
      // A 502 may carry the draft the model already wrote — the filing failed,
      // not the drafting. Showing it is what makes the retry cheap, and it is
      // also the only copy of that text that exists anywhere.
      const body = (err as ApiError)?.body as { draft?: Draft } | undefined;
      if (body && body.draft) setDraft(body.draft);
    } finally {
      if (slowTimer.current) window.clearTimeout(slowTimer.current);
      slowTimer.current = null;
      setSlow(false);
      setFiling(false);
    }
  }, [brief, ready, source]);

  // --- filed: the link, which is the whole deliverable --------------------
  if (filed) {
    return (
      <div className="nt-pane nt-done">
        <div className="nt-filed">
          <span className="nt-filed-chip">Filed</span>
          <a
            className="nt-filed-link"
            href={filed.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {filed.slug || filed.id} — open in {filed.source_label}
          </a>
        </div>
        <p className="nt-filed-name">{filed.name}</p>
        {!!(filed.criteria || []).length && (
          <ul className="nt-criteria">
            {(filed.criteria || []).map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        )}
        {ingestOn && (
          <p className="nt-note">
            Ticket ingestion is on for this flock, so a session may start for this
            ticket on the next poll. You can also start one now from Intake.
          </p>
        )}
        <div className="modal-actions">
          <button
            type="button"
            className="linklike"
            onClick={() => {
              setFiled(null);
              setBrief("");
              // Focus lands back in the box on the next paint, after the box
              // exists again.
              window.setTimeout(() => briefRef.current?.focus(), 0);
            }}
          >
            File another
          </button>
          <button type="button" onClick={() => openDialogFor("intake", "tickets")}>
            Open Intake
          </button>
          <button type="button" onClick={closeDialog}>
            Done
          </button>
        </div>
      </div>
    );
  }

  // --- nothing to file into ----------------------------------------------
  if (sources && !sources.length) {
    return (
      <div className="nt-pane nt-empty">
        <p className="nt-note">
          No ticketing source is connected, so there is nowhere to file a ticket.
          Connect one under Intake → Tickets and this tab will use it.
        </p>
        <div className="modal-actions">
          <button type="button" onClick={() => openDialogFor("intake", "tickets")}>
            Open Intake
          </button>
        </div>
      </div>
    );
  }

  const usable = (sources || []).filter((s) => s.can_create);

  return (
    <div className="nt-pane">
      <label className="nt-label" htmlFor="nt-brief">
        What needs doing?
      </label>
      <textarea
        id="nt-brief"
        ref={briefRef}
        className="nt-brief"
        value={brief}
        maxLength={MAX_BRIEF}
        spellCheck={false}
        // readOnly rather than disabled: a disabled textarea loses focus to the
        // body, so the caret would jump out of the box the moment the button
        // was pressed and the text would stop being selectable while the user
        // waits to see what it produced.
        readOnly={filing}
        placeholder={
          "e.g. the login page hangs for SSO users on slow connections — it should " +
          "time out and show a retry instead"
        }
        onChange={(e) => {
          setBrief(e.target.value);
          if (error) setError("");
        }}
        onKeyDown={(e) => {
          // Enter is a newline here: this is a brief, not a name field, and the
          // useful ones run to two or three lines. Ctrl/Cmd+Enter files, which
          // is the same gesture the session tab uses to create.
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            e.stopPropagation();
            file();
          }
        }}
      />
      <p className="nt-help">
        MindFlock writes the ticket — a title, a description and acceptance
        criteria — and files it. There is no form: the tracker already has one,
        and it is one click away for anything this cannot say.
      </p>

      {sourcesError ? (
        <p className="error">{sourcesError}</p>
      ) : (
        <div className="nt-source-row">
          <label className="nt-label" htmlFor="nt-source">
            File on
          </label>
          <select
            id="nt-source"
            className="nt-source"
            value={source}
            disabled={filing || !sources}
            onChange={(e) => setSource(e.target.value)}
          >
            <option value="">
              {sources ? (usable.length ? "Choose a source…" : "No source can accept a ticket") : "Loading…"}
            </option>
            {(sources || []).map((s) => (
              // Sources that can't accept a ticket stay on the list, disabled:
              // "Jira isn't here" and "this Jira source has no project set" are
              // different problems, and only one of them is the user's to fix.
              <option key={s.key} value={s.key} disabled={!s.can_create}>
                {s.label}
                {s.can_create ? "" : " — unavailable"}
              </option>
            ))}
          </select>
        </div>
      )}
      {chosen && !chosen.can_create && <p className="nt-note">{chosen.blocker}</p>}
      {!chosen && !!usable.length && !sourcesError && (
        <p className="nt-note">Pick where the ticket should be filed.</p>
      )}
      {ingestOn && (
        <p className="nt-note">
          Ticket ingestion is on, so a session may start for this ticket
          automatically once it is filed.
        </p>
      )}

      {!!error && <p className="error">{error}</p>}
      {draft && (
        /* The filing failed after the model turn. The draft is shown rather
           than thrown away because it is the expensive half and, right now, the
           only copy of that text anywhere. */
        <div className="nt-draft">
          <p className="nt-draft-head">Drafted, but not filed:</p>
          <p className="nt-filed-name">{draft.name}</p>
          <pre className="nt-draft-body">{draft.description}</pre>
        </div>
      )}

      <div className="modal-actions">
        <button type="button" className="linklike" onClick={closeDialog}>
          Cancel
        </button>
        <button type="button" disabled={!ready} onClick={file}>
          {filing ? (slow ? "Still writing…" : "Filing…") : "Create ticket"}
        </button>
      </div>
    </div>
  );
}
