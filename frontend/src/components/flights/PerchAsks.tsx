/** The parts of a stop parked on you that ask more than a Done (lib/stopAsks):
 *
 *  - `ReviewFindings` — a Review out of fix rounds: the open findings,
 *    preformatted, in a scrolling box of capped height.
 *  - `OwedChecks` — the perch asking for the checks Verify stops left for the
 *    end: each Verify stop's title, its items (the Verify confirm's own step
 *    list, `HumanSteps`), and a small "This one didn't work" that asks for one
 *    line (the Verify confirm's own note row, `NotVerifiedNote`) and posts
 *    the plan's confirm with verified false — the route goes back to that
 *    Verify stop.
 *  - `OwedNote` — the quiet "2 checks owed to you at the end" next to a route
 *    still flying: its tooltip (and a press) lists the items by stop. Muted,
 *    never the amber of a stop waiting on you.
 *
 * The buttons themselves (Give it another round · Carry on · Done — they all
 * work) are the hosts': FlightStrip's `YouStepActions`, the stop dialog's
 * remedies, the bell's row — each reads `askOffers`. */

import { useState, type JSX } from "react";
import { HumanSteps, NotVerifiedNote } from "../behaviors/VerifyConfirm";
import { confirmVerified } from "../../lib/verifyActions";
import { refreshFlights } from "../../lib/flightsApi";
import { OWED_FAILED, OWED_FAILED_TITLE, owedTip, type OwedGroup } from "../../lib/stopAsks";
import "./Flights.css";

const stop = (e: { stopPropagation(): void }) => e.stopPropagation();

/** A Review's open findings, as the reviewer wrote them. */
export function ReviewFindings({ findings }: { findings: string }): JSX.Element | null {
  if (!findings) return null;
  return (
    <pre className="fl-findings" aria-label="Findings still open" tabIndex={0}>
      {findings}
    </pre>
  );
}

/** The checks owed to you, grouped by the Verify stop that left them. */
export function OwedChecks({ groups, session }: { groups: readonly OwedGroup[]; session?: string }): JSX.Element | null {
  if (!groups.length) return null;
  return (
    <div className="fl-owed" role="group" aria-label="Checks for you" onClick={stop} onMouseDown={stop} data-no-drag>
      {groups.map((g) => (
        <OwedGroupRow key={g.step} group={g} session={session} />
      ))}
    </div>
  );
}

function OwedGroupRow({ group, session }: { group: OwedGroup; session?: string }) {
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const steps = (group.items.length ? group.items : ["Confirm it works"]).map((text, i) => ({ id: group.step + ":" + i, text, expect: "" }));
  const send = async () => {
    if (busy || !note.trim() || !group.plan) return;
    setBusy(true);
    try {
      if (await confirmVerified(group.plan, false, note.trim(), { session })) {
        setAsking(false);
        setNote("");
      }
    } finally {
      setBusy(false);
      void refreshFlights();
    }
  };
  return (
    <section className="fl-owed-group" data-owed={group.step}>
      <h4 className="fl-owed-head">{group.title}</h4>
      <HumanSteps steps={steps} />
      {asking ? (
        <NotVerifiedNote
          note={note}
          onNote={setNote}
          busy={busy}
          sending={busy}
          required
          autoFocus
          placeholder="What did you see?"
          sendLabel="It didn't work"
          sendTitle={OWED_FAILED_TITLE}
          onSend={() => void send()}
          onCancel={() => {
            setAsking(false);
            setNote("");
          }}
        />
      ) : (
        <button type="button" className="linklike fl-owed-failed" disabled={!group.plan} title={OWED_FAILED_TITLE} onClick={() => setAsking(true)}>
          {OWED_FAILED}
        </button>
      )}
    </section>
  );
}

/** "2 checks owed to you at the end": hover lists them by stop; a press
 * opens the same list under it. `note` "" draws nothing. */
export function OwedNote({ note, groups }: { note: string; groups: readonly OwedGroup[] }): JSX.Element | null {
  const [open, setOpen] = useState(false);
  if (!note) return null;
  return (
    <span className="fl-owed-note-wrap" onClick={stop} onMouseDown={stop} data-no-drag>
      <button type="button" className="linklike fl-owed-note" aria-expanded={open} title={owedTip(groups)} onClick={() => setOpen((v) => !v)}>
        {note}
      </button>
      {open && (
        <span className="fl-owed-list" role="list" aria-label="Checks owed to you at the end">
          {groups.map((g) => (
            <span key={g.step} role="listitem" className="fl-owed-list-group">
              <strong>{g.title}</strong>
              {(g.items.length ? g.items : ["Confirm it works"]).map((t, i) => (
                <span key={i} className="fl-owed-list-item">
                  {t}
                </span>
              ))}
            </span>
          ))}
        </span>
      )}
    </span>
  );
}
