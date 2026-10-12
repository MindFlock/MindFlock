/** The New dialog's action row: page 1's three ways out (one session, or an
 * Intake batch's run), or the form's Back / Create, with the error line
 * beside them. */

import type { Dispatch, SetStateAction } from "react";
import { startLabel } from "../../../lib/runStart";
import type { RunDraft } from "../NewList";

export function FooterActions({
  error,
  page,
  setPage,
  runMode,
  describing,
  describeSlow,
  draft,
  runDescribe,
  startRun,
  startNow,
}: {
  error: string;
  page: number;
  setPage: Dispatch<SetStateAction<number>>;
  runMode: boolean;
  describing: boolean;
  describeSlow: boolean;
  draft: RunDraft;
  runDescribe(mode?: "fill" | "start"): Promise<void>;
  startRun(): Promise<void>;
  startNow(): void;
}) {
  return (
    <div className="modal-actions nf-actions">
      <p id="new-error" className="error">{error}</p>
      {page === 1 ? (
        <>
          {/* THREE actions, in increasing order of commitment, left to
              right — and the row has to say which is which on its own,
              because the previous labels ("Skip — fill it in myself",
              "✨ Fill in the form", "Start session now") were all written
              from inside the app's own vocabulary. "The form" is page 2,
              which nobody has seen yet; "skip" does not say what is being
              skipped or what you get instead. These name the OUTCOME.

              The escape is first, quiet, and pushed to the far left by its
              own margin so it reads as "not one of these two". It cannot be
              dropped: page 1 is where every opening lands, so this is the
              only route to the form for someone who does not want to
              describe anything — or who has no coding CLI installed to
              describe it to. */}
          <button
            type="button"
            id="new-describe-skip"
            className="nf-quiet"
            title="Go straight to the full form and choose the folder and options yourself. Nothing is read, and no model runs."
            onClick={() => setPage(2)}
          >
            Set it up myself instead
          </button>
          {/* Review is for ONE session: a list (or a split) starts from the
              rows and choices above, so in run mode the button is not drawn
              at all rather than drawn and then refusing. One fewer button
              on the busiest version of this page. */}
          {!runMode && (
          <button
            // type="button" is load-bearing, not tidiness: the default is
            // "submit", so without it this button creates a session out of
            // whatever the form happens to be holding.
            type="button"
            id="new-describe-go"
            // Disabled only while a turn is in flight — NEVER for a sentence
            // that is too short. A greyed-out control that will not say why
            // is furniture explaining itself; both this and Enter go through
            // describeBlockReason instead and get the sentence.
            disabled={describing}
            aria-busy={describing || undefined}
            title="Work out the folder, name and first instruction, then show them to you so you can change anything before the session is created."
            onClick={() => void runDescribe("fill")}
          >
            {describing ? (
              <>
                {/* The ring AND a changed label: a cold CLI start plus a
                    real turn runs to ~25s, and a button that only spins
                    reads as a hang long before the server's own timeout
                    would say anything. */}
                <span className="btn-spin" aria-hidden="true" />{" "}
                {describeSlow ? "Still reading…" : "Reading…"}
              </>
            ) : (
              <>
                Review details first{" "}
                {/* What Enter does, shown rather than described. The
                    sentence that used to say it in words sat in the header
                    and explained the wrong page. */}
                <span className="nf-key" aria-hidden="true">
                  ↵
                </span>
              </>
            )}
          </button>
          )}
          {/* The "don't make me read anything" path. Accent, because it is
              the one this page exists for, and a BUTTON rather than the
              Enter key on purpose: Enter in the box means "read this", it
              has meant that since the box existed, and a key that creates a
              session in a folder nobody has looked at is the one gesture
              this whole feature has been careful not to build. The single
              exception it does not skip is a folder that does not exist —
              see immediateStartBlockReason. */}
          <button
            type="button"
            id="new-describe-start"
            disabled={describing || draft.starting}
            aria-busy={draft.starting || undefined}
            title={
              runMode
                ? "Start them now. MindFlock queues the rest and ships each one as chosen above, and the bell shows anything that needs you."
                : "Create the session right now from what you typed, without showing you the details first."
            }
            onClick={runMode ? () => void startRun() : startNow}
          >
            {draft.starting
              ? "Starting…"
              : startLabel(draft.listMode ? draft.count : 1)}
          </button>
        </>
      ) : (
        <>
          {/* Back, not Cancel: page 1 still holds the sentence, and a
              create that has not happened yet is not something to undo. */}
          <button
            type="button"
            id="new-back"
            className="linklike"
            onClick={() => setPage(1)}
          >
            ← Back
          </button>
          <button
            type="submit"
            disabled={draft.starting || describing}
            aria-busy={draft.starting || undefined}
            title={describing ? "Waiting for the description to be read" : undefined}
          >
            {draft.starting ? "Starting…" : "Create"}
          </button>
        </>
      )}
    </div>
  );
}
