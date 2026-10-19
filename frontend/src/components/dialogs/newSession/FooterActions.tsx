/** The New dialog's action row: page 1's ONE primary (Start ↵) with "More
 * options" and "Let the agent pick the folder…" beside it (or an Intake batch's
 * Start N), or the form's ← Back / Start, with the error line beside them.
 * "Save as template…" lives in the form's More options. */

import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { startLabel } from "../../../lib/runStart";
import type { RunDraft } from "../NewList";
import { START_LABEL, elapsedLabel } from "../newSession";

/** Seconds since `since`, ticking once a second while mounted. */
export function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const h = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(h);
  }, []);
  return <span className="nf-elapsed">{elapsedLabel((now - since) / 1000)}</span>;
}

/** The ring and a word on the button that was pressed; the line under the
 * box ("Asking Claude Code to fill in the details… 0:04 (up to 30 s)") says
 * who is doing what, and for how long at most. */
function Reading({ slow }: { slow: boolean }) {
  return (
    <>
      <span className="btn-spin" aria-hidden="true" /> {slow ? "Still asking…" : "Asking…"}
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
  draft,
  runDescribe,
  startRun,
  startNow,
  setUpMyself,
  startBlocked = "",
}: {
  error: string;
  page: number;
  setPage: Dispatch<SetStateAction<number>>;
  runMode: boolean;
  describing: boolean;
  describeSlow: boolean;
  /** Which button started the read in flight. */
  describeMode: "fill" | "start";
  draft: RunDraft;
  runDescribe(mode?: "fill" | "start"): Promise<void>;
  startRun(): Promise<void>;
  startNow(): void;
  /** To the form, carrying what was typed as the Task. */
  setUpMyself(): void;
  /** Why Start / the form's Start can't go yet ("" = it can): the folder
   * named doesn't exist. Said on the button's tooltip; the red line under
   * the folder says it on screen. */
  startBlocked?: string;
}) {
  return (
    <>
      <div className="modal-actions nf-actions">
        {/* Announced when it lands: it is why Start did nothing. */}
        <p id="new-error" className="error" role="alert">{error}</p>
        {page === 1 ? (
          <>
            {/* ONE primary. "More options" (the full form, nothing read) is
                quiet and far left; "Let the agent pick the folder…" — a model
                turn that reads the sentence and fills the form in for review —
                is secondary, and named for the one choice it makes instead of
                you (the "Runs in" folder); Start (accent, ↵ — what Enter does)
                starts it now where the "Runs in" line says. */}
            <button
              type="button"
              id="new-describe-skip"
              className="nf-quiet"
              data-tour="new-more"
              title="The full form — folder, agent, task, name and Fast-track — with what you typed as the task"
              onClick={setUpMyself}
            >
              More options
            </button>
            {/* For ONE session only: a list (or a split) starts from the rows
                and choices above, so in run mode it is not drawn. */}
            {!runMode && (
              <button
                // type="button" is load-bearing: the default is "submit",
                // which would create a session from whatever the form holds.
                type="button"
                id="new-describe-go"
                disabled={describing}
                aria-busy={(describing && describeMode === "fill") || undefined}
                title="Instead of the Runs in folder: your agent reads what you typed and picks the folder, name and options; you check them before anything starts."
                onClick={() => void runDescribe("fill")}
              >
                {describing && describeMode === "fill" ? <Reading slow={describeSlow} /> : "Let the agent pick the folder…"}
              </button>
            )}
            {/* Never waits on the agent: pressed during a read, it drops the
                read and starts from the words as typed. */}
            <button
              type="button"
              id="new-describe-start"
              data-tour="new-start"
              disabled={draft.starting || (runMode && describing) || (!runMode && !!startBlocked)}
              aria-busy={(describing && describeMode === "start") || draft.starting || undefined}
              title={
                runMode
                  ? "Start them now. MindFlock queues the rest and ships each one as chosen above, and the bell shows anything that needs you."
                  : startBlocked || "Start it now in the folder shown, with what you typed as its task (Enter)"
              }
              onClick={runMode ? () => void startRun() : startNow}
            >
              {describing && describeMode === "start" ? (
                <Reading slow={describeSlow} />
              ) : draft.starting ? (
                "Starting…"
              ) : draft.listMode && draft.count >= 2 ? (
                startLabel(draft.count)
              ) : (
                <>
                  {START_LABEL}{" "}
                  <span className="nf-key" aria-hidden="true">
                    ↵
                  </span>
                </>
              )}
            </button>
          </>
        ) : (
          <>
            {/* Back, not Cancel: page 1 still holds the sentence (and its
                Clear), and a start that has not happened yet is not
                something to undo. */}
            <button type="button" id="new-back" className="nf-quiet" onClick={() => setPage(1)}>
              ← Back
            </button>
            <button
              type="submit"
              id="new-create"
              data-tour="new-start"
              disabled={draft.starting || describing || !!startBlocked}
              aria-busy={draft.starting || undefined}
              title={describing ? "Waiting for the agent to fill in the details" : startBlocked || "Start it now (Enter)"}
            >
              {draft.starting ? (
                "Starting…"
              ) : (
                <>
                  {START_LABEL}{" "}
                  <span className="nf-key" aria-hidden="true">
                    ↵
                  </span>
                </>
              )}
            </button>
          </>
        )}
      </div>
    </>
  );
}

/** More options → "Save as template…": the button, and the inline name row
 * it opens. Saves the WHOLE form — folder, agent, works in, task, Fast-track. */
export function SaveTemplateRow({ saveTpl }: { saveTpl: SaveTemplateUi }) {
  return (
    <div className="nf-save-tpl-wrap">
      {saveTpl.name === null ? (
        <button
          type="button"
          id="new-save-tpl"
          className="linklike"
          aria-expanded={false}
          title="Save this whole setup — folder, agent, where it works, task and Fast-track — to start again in one click or on a schedule"
          onClick={() => saveTpl.setName("")}
        >
          Save as template…
        </button>
      ) : (
        /* Inline, not a prompt(): Electron has no window.prompt. */
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
              // starts the session) and Escape must not close the dialog.
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
              "Saves the folder, agent, where it works, task and Fast-track. Run it from Templates, now or on a schedule."}
          </span>
        </div>
      )}
    </div>
  );
}
