/** The New dialog's action row: page 1's three ways out (one session, or an
 * Intake batch's run), or the form's Save as template… / Back / Create, with
 * the error line beside them. */

import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { startLabel } from "../../../lib/runStart";
import type { RunDraft } from "../NewList";
import { elapsedLabel } from "../newSession";

/** Seconds since `since`, ticking once a second while mounted. */
function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const h = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(h);
  }, []);
  return <span className="nf-elapsed">{elapsedLabel((now - since) / 1000)}</span>;
}

/** The ring, a word and the clock — on the button that was pressed. */
function Reading({ slow, since }: { slow: boolean; since: number }) {
  return (
    <>
      <span className="btn-spin" aria-hidden="true" /> {slow ? "Still reading…" : "Reading…"}{" "}
      <Elapsed since={since} />
    </>
  );
}

export interface SaveTemplateUi {
  /** The inline name row is open (null = closed). */
  name: string | null;
  setName: Dispatch<SetStateAction<string | null>>;
  busy: boolean;
  save(): void;
  /** Why it can't be saved yet ("" = it can). */
  blocked: string;
}

export function FooterActions({
  error,
  page,
  setPage,
  runMode,
  describing,
  describeSlow,
  describeMode,
  describeSince,
  draft,
  runDescribe,
  startRun,
  startNow,
  setUpMyself,
  onClear,
  saveTpl,
}: {
  error: string;
  page: number;
  setPage: Dispatch<SetStateAction<number>>;
  runMode: boolean;
  describing: boolean;
  describeSlow: boolean;
  /** Which button started the read in flight. */
  describeMode: "fill" | "start";
  /** When it started (epoch ms). */
  describeSince: number;
  draft: RunDraft;
  runDescribe(mode?: "fill" | "start"): Promise<void>;
  startRun(): Promise<void>;
  startNow(): void;
  /** To the form, carrying what was typed as the Task. */
  setUpMyself(): void;
  /** Clear the draft (shown on the form while something typed is in it). */
  onClear?: () => void;
  saveTpl: SaveTemplateUi;
}) {
  return (
    <>
      {page === 2 && saveTpl.name !== null && (
        /* Inline, not a prompt(): Electron has no window.prompt. Saves the
           WHOLE form — folder, agent, works in, task, fast-track. */
        <div className="nf-save-tpl" id="new-save-tpl-row" role="group" aria-label="Save as template">
          <input
            id="new-save-tpl-name"
            type="text"
            autoFocus
            autoComplete="off"
            spellCheck={false}
            placeholder="Template name, e.g. nightly-deps"
            aria-label="Template name"
            value={saveTpl.name}
            onChange={(e) => saveTpl.setName(e.target.value)}
            onKeyDown={(e) => {
              // Kept to this row: Enter must not submit the form (that
              // creates the session) and Escape must not close the dialog.
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                if (!saveTpl.blocked && !saveTpl.busy) saveTpl.save();
              } else if (e.key === "Escape") {
                e.preventDefault();
                saveTpl.setName(null);
              }
            }}
          />
          <button
            type="button"
            id="new-save-tpl-save"
            disabled={!!saveTpl.blocked || saveTpl.busy}
            onClick={saveTpl.save}
          >
            {saveTpl.busy ? "Saving…" : "Save"}
          </button>
          <button type="button" className="linklike" onClick={() => saveTpl.setName(null)}>
            Cancel
          </button>
          <span className="muted nf-save-tpl-hint">
            {saveTpl.blocked ||
              "Saves the folder, agent, where it works, task and fast-track. Run it from Templates, now or on a schedule."}
          </span>
        </div>
      )}
      <div className="modal-actions nf-actions">
        <p id="new-error" className="error">{error}</p>
        {page === 1 ? (
          <>
            {/* THREE actions, in increasing order of commitment. The escape is
                first, quiet, and pushed to the far left: page 1 is where every
                opening lands, so it is the only route to the form for someone
                who does not want to describe anything — or has no agent
                installed to describe it to. */}
            <button
              type="button"
              id="new-describe-skip"
              className="nf-quiet"
              title="Go to the form with what you typed as the task, and choose the folder and options yourself. Nothing is read."
              onClick={setUpMyself}
            >
              Set it up myself instead
            </button>
            {/* Review is for ONE session: a list (or a split) starts from the
                rows and choices above, so in run mode it is not drawn. It is
                the PRIMARY button, because it is what Enter does — the ↵ and
                the accent sit on the same control. */}
            {!runMode && (
              <button
                // type="button" is load-bearing: the default is "submit",
                // which would create a session from whatever the form holds.
                type="button"
                id="new-describe-go"
                disabled={describing}
                aria-busy={(describing && describeMode === "fill") || undefined}
                title="Your agent fills in the folder, name and task; you check them before anything is created."
                onClick={() => void runDescribe("fill")}
              >
                {describing && describeMode === "fill" ? (
                  <Reading slow={describeSlow} since={describeSince} />
                ) : (
                  <>
                    Review details first{" "}
                    <span className="nf-key" aria-hidden="true">
                      ↵
                    </span>
                  </>
                )}
              </button>
            )}
            {/* Never waits on the agent: pressed during a Review read, it drops
                the read and starts from the words as typed. */}
            <button
              type="button"
              id="new-describe-start"
              disabled={draft.starting || (runMode && describing)}
              aria-busy={(describing && describeMode === "start") || draft.starting || undefined}
              title={
                runMode
                  ? "Start them now. MindFlock queues the rest and ships each one as chosen above, and the bell shows anything that needs you."
                  : "Start the session now in the folder shown, with what you typed as its task — nothing to review."
              }
              onClick={runMode ? () => void startRun() : startNow}
            >
              {describing && describeMode === "start" ? (
                <Reading slow={describeSlow} since={describeSince} />
              ) : draft.starting ? (
                "Starting…"
              ) : (
                startLabel(draft.listMode ? draft.count : 1)
              )}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              id="new-save-tpl"
              className="nf-quiet"
              aria-expanded={saveTpl.name !== null}
              title="Save this whole setup — folder, agent, where it works, task and fast-track — to start again in one click or on a schedule"
              onClick={() => saveTpl.setName((cur) => (cur === null ? "" : null))}
            >
              Save as template…
            </button>
            {onClear && (
              <button
                type="button"
                id="new-clear-form"
                className="linklike"
                title="Empty the form and start over — what you type here otherwise stays until you Create"
                onClick={onClear}
              >
                Clear
              </button>
            )}
            {/* Back, not Cancel: page 1 still holds the sentence, and a
                create that has not happened yet is not something to undo. */}
            <button type="button" id="new-back" className="linklike" onClick={() => setPage(1)}>
              ← Back
            </button>
            <button
              type="submit"
              id="new-create"
              disabled={draft.starting || describing}
              aria-busy={draft.starting || undefined}
              title={describing ? "Waiting for the description to be read" : undefined}
            >
              {draft.starting ? "Starting…" : "Create"}
            </button>
          </>
        )}
      </div>
    </>
  );
}
