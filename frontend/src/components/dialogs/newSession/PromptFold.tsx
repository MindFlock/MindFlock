/** The New form's Task box (the prompt the agent starts on — on the form,
 * never folded: it is the one thing a session is for) with a picker that
 * inserts a saved prompt, and the Plan first row that lives under More
 * options.
 *
 * Saving happens in ONE place: the footer's "Save as template…" saves the
 * whole form. Saved prompts themselves (text you send to a running session)
 * are added and removed in the Prompts dialog — Manage… opens it and hands
 * back to this form on Close. */

import type { Dispatch, SetStateAction } from "react";
import { BUILTIN_PRESETS, findPreset, type Preset } from "../../../lib/presets";
import { useUi } from "../../../state/store";

type Set<T> = Dispatch<SetStateAction<T>>;

export interface TaskFieldProps {
  prompt: string;
  setPrompt: Set<string>;
  presetValue: string;
  setPresetValue: Set<string>;
  savedPresets: Preset[];
}

export function TaskField({ prompt, setPrompt, presetValue, setPresetValue, savedPresets }: TaskFieldProps) {
  return (
    <div id="new-prompt-fold" className="nf-task">
      <div className="nf-task-head">
        <label htmlFor="new-prompt">
          Task <span className="muted">— what the agent starts on; blank starts it idle</span>
        </label>
        <span className="preset-row">
          <select
            id="new-preset"
            aria-label="Insert a saved prompt"
            title="Put a saved prompt in the Task box (you can edit it after)"
            value={presetValue}
            onChange={(e) => {
              setPresetValue(e.target.value);
              const p = findPreset(e.target.value);
              if (p) setPrompt(p.prompt);
            }}
          >
            <option value="">Insert a saved prompt…</option>
            {BUILTIN_PRESETS.length > 0 && (
              <optgroup label="Built-in">
                {BUILTIN_PRESETS.map((p) => (
                  <option key={"b:" + p.name} value={"b:" + p.name} title={p.prompt}>
                    {p.name}
                  </option>
                ))}
              </optgroup>
            )}
            {savedPresets.length > 0 && (
              <optgroup label="Saved">
                {savedPresets.map((p) => (
                  <option key={"u:" + p.name} value={"u:" + p.name} title={p.prompt}>
                    {p.name}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <button
            type="button"
            id="preset-manage"
            className="linklike"
            title="Add or remove saved prompts — you come back here on Close"
            // Not a hand-off that loses the form: Close in Prompts brings
            // this dialog back as it was (newSessionResume).
            onClick={() => useUi.getState().openDialogFor("prompts", "new")}
          >
            Manage…
          </button>
        </span>
      </div>
      <textarea
        id="new-prompt"
        rows={3}
        autoComplete="off"
        spellCheck={false}
        placeholder="e.g. add a section about setup to the README"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
      />
    </div>
  );
}

export function PlanFirstRow({
  prompt,
  planOk,
  planFirst,
  setPlanFirst,
}: {
  prompt: string;
  planOk: boolean;
  planFirst: boolean;
  setPlanFirst: Set<boolean>;
}) {
  return (
    <label
      className={"check" + (planOk ? "" : " disabled")}
      id="new-plan-first-row"
      title={
        planOk
          ? "The agent first lists every file it intends to create, modify or delete, with a one-line " +
            "intent for each, then waits for your go-ahead. Open the session's Map tab to see that plan " +
            "and its blast radius, red-zone anything it shouldn't touch, and press Go."
          : "Only an agent that can declare a plan gets the Map's plan review and Go button — pick Claude " +
            "for Plan first."
      }
    >
      <input
        type="checkbox"
        id="new-plan-first"
        checked={planFirst && planOk}
        disabled={!planOk}
        onChange={(e) => setPlanFirst(e.target.checked)}
      />
      Plan first{" "}
      <span className="muted">
        {!planOk
          ? "(needs an agent with plan support — Claude)"
          : prompt.trim()
            ? "(list files + intent, then wait for Go on the Map tab)"
            : "(takes effect with a task)"}
      </span>
    </label>
  );
}
