/** Intake → Tickets → **Merge into…** — fold one duplicate ticket into another
 * and delete it.
 *
 * The surface this exists for: two people file the same task, and the queue now
 * has two rows that will each become a session. The fix has to happen in the
 * TRACKER, because that is where both filers will go back to look — so this
 * writes to Shortcut/Jira/Linear/GitHub, and the deletion is a real deletion.
 *
 * THE SHAPE, and why it is a drawer rather than a dialog. These rows already
 * live inside an `aria-modal` Intake dialog. Opening a second window from
 * inside one — a native `confirm()` or a nested overlay — is the exact mistake
 * Verify wrote three paragraphs about (VerifyDialog.tsx): it is painted by the
 * OS or by the wrong layer, it steals focus from a dialog that still thinks it
 * owns it, and on a phone it is a system sheet over an app sheet. So the
 * confirmation happens in place, under the row it is about, in this app's own
 * paint, and the row stays on screen the whole time.
 *
 * WHY THE BUTTON ARMS. Everything else on this panel is reversible: a session
 * started by mistake gets closed, a bucket hidden by mistake gets re-added. This
 * one reaches into somebody else's tracker and destroys a ticket other people
 * can see, and there is no undo anywhere in this app or in most of those
 * trackers. Two presses, with the second one naming what it is about to delete,
 * is the cheapest honest guard — and it is cheap precisely because nobody
 * merges tickets in a hurry.
 *
 * WHAT IT DOES NOT DO: touch the session. A ticket can have a MindFlock session
 * open on it, and merging the ticket away does not kill that session — the work
 * in it is real, possibly uncommitted, and not this button's to throw out. The
 * drawer says so out loud instead, because a session whose ticket no longer
 * exists is confusing in a way only the warning can prevent.
 */

import { useState } from "react";
import { api } from "../../api/client";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import { mergeOutcome, mergeTargets, type MergeCandidate, type MergeResult } from "./merge";

/** The button that opens the drawer, in the row's ACTION COLUMN under **Begin
 * work** — the quiet weight `ik-start-again` already uses for a row's secondary
 * action.
 *
 * It was a chip in the meta line first, on the theory that a rare action should
 * not cost a common row any height. That theory was wrong twice over: the meta
 * line wraps, so on any row with a real skip reason it took a line anyway — and
 * a control sitting among `6d old` and `not in an ingest state` inherits their
 * grey, borderless, passive reading and stops looking like something you can
 * press. It was reported missing by someone looking directly at it. Actions go
 * where the row keeps its actions. */
export function MergeButton({
  reference,
  open,
  onToggle,
}: {
  reference: string;
  open: boolean;
  onToggle(): void;
}) {
  return (
    <button
      type="button"
      className={"test-btn ik-merge-btn" + (open ? " on" : "")}
      aria-expanded={open}
      title={
        "Fold " +
        reference +
        " into another ticket from this source — its description, comments and " +
        "files move across and " +
        reference +
        " is then deleted from the tracker."
      }
      onClick={onToggle}
    >
      {/* The label does NOT flip to "Cancel" when open. The drawer already
          carries a Cancel, sitting where it belongs — paired with the
          destructive button it is the alternative to — and two Cancels four
          inches apart is a question about which one means what. The button is
          highlighted and `aria-expanded`, which is how every other toggle in
          this dialog says it is open. */}
      Merge into…
    </button>
  );
}

export function MergeDrawer({
  row,
  all,
  hasSession,
  onDone,
  onClose,
}: {
  row: MergeCandidate;
  /** Every ticket the panel is holding — the candidate targets are filtered out
   * of this (same source, not itself) rather than passed in pre-filtered, so
   * the one rule lives in `merge.ts` where it is tested. */
  all: MergeCandidate[];
  /** Whether a MindFlock session is open on the ticket about to be deleted. */
  hasSession?: boolean;
  /** Called after a merge that actually reached the tracker, to relist. */
  onDone(): void;
  onClose(): void;
}) {
  const [query, setQuery] = useState("");
  const [target, setTarget] = useState("");
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  const candidates = mergeTargets(all, row, query);
  const picked = candidates.find((c) => String(c.id) === target);
  // Narrowing the list must not silently keep a selection that is no longer in
  // it — an armed button naming a ticket the user can no longer see is the one
  // way this could delete the wrong thing.
  const effective = picked ? target : "";

  const submit = async () => {
    if (!effective || busy) return;
    setBusy(true);
    try {
      const r = await api<MergeResult>("/api/tickets/merge", {
        json: { source: row.source, from: row.id, into: effective },
      });
      const { tone, text } = mergeOutcome(r);
      // A merge that half-landed goes to the bottom-right card, not to a 1.4s
      // toast: "it could not be deleted, go and delete it by hand" is an
      // instruction, and an instruction has to outlive the animation.
      if (tone === "ok") toast(text);
      else errorPop("Merged, with something left to do", text);
      onClose();
      onDone();
    } catch (err) {
      errorPop(
        "Merge failed — " + row.slug + " is untouched",
        (err as Error).message || "the server gave no reason"
      );
      setBusy(false);
      setArmed(false);
    }
  };

  return (
    <div className="ik-merge">
      <label className="ik-merge-pick">
        <span className="ik-merge-label">Merge {row.slug} into</span>
        <input
          type="text"
          className="ik-merge-filter"
          // Short on purpose: it shares a line with the picker, and a
          // placeholder that names all three fields only ever renders as
          // "Filter by ticket, title or st".
          placeholder="Filter…"
          value={query}
          autoComplete="off"
          onChange={(e) => {
            setQuery(e.target.value);
            setArmed(false);
          }}
        />
        <select
          className="ik-merge-target"
          value={effective}
          disabled={busy}
          aria-label={"Ticket to merge " + row.slug + " into"}
          onChange={(e) => {
            setTarget(e.target.value);
            setArmed(false);
          }}
        >
          <option value="">
            {candidates.length
              ? "Pick the ticket that survives…"
              : query
                ? "No other ticket here matches that"
                : "No other ticket on this source"}
          </option>
          {candidates.map((c) => (
            <option key={c.id} value={String(c.id)}>
              {c.slug + " — " + (c.name || "untitled") + (c.bucket ? "  ·  " + c.bucket : "")}
            </option>
          ))}
        </select>
      </label>
      <p className="ik-merge-note">
        {picked ? (
          <>
            Everything on <strong>{row.slug}</strong> — its description, acceptance
            criteria, comments and attached files — is appended to{" "}
            <strong>{picked.slug}</strong>, a comment on {picked.slug} records where it
            came from, and then <strong>{row.slug} is deleted from the tracker</strong>.
            That last part cannot be undone from here or, on most trackers, at all.
          </>
        ) : (
          <>
            Only tickets from this same source can receive it: the files cannot follow a
            ticket into another tracker, and the surviving ticket keeps its own queue's
            repo and agent.
          </>
        )}
      </p>
      {hasSession && picked ? (
        <p className="ik-merge-warn">
          A session is open on {row.slug}. It keeps running on its own workspace and
          branch — merging the ticket away does not close it, and nothing here touches
          the work in it. Close it yourself if it is the duplicate you are dropping.
        </p>
      ) : null}
      <div className="ik-merge-actions">
        <button type="button" className="test-btn" disabled={busy} onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className={"btn-primary ik-merge-go" + (armed ? " armed" : "")}
          disabled={!effective || busy}
          title={
            effective
              ? "Deletes " + row.slug + " once its content is on " + (picked?.slug || "")
              : "Pick the ticket that survives first"
          }
          onClick={() => (armed ? submit() : setArmed(true))}
        >
          {busy
            ? "Merging…"
            : armed
              ? "Confirm — delete " + row.slug
              : "Merge and delete " + row.slug}
        </button>
      </div>
    </div>
  );
}
