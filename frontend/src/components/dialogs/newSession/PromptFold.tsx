/** The New form's Task box (the prompt the agent starts on — on the form,
 * never folded: it is the one thing a session is for) with a picker that
 * inserts a saved prompt, and the Plan first row that lives under More
 * options.
 *
 * Saving happens in ONE place: the footer's "Save as template…" saves the
 * whole form. Saved prompts themselves (text you send to a running session)
 * are added and removed in the Prompts dialog — Manage… opens it and hands
 * back to this form on Close. */

import { useLayoutEffect, useRef, type Dispatch, type SetStateAction } from "react";
import { BUILTIN_PRESETS, findPreset, type Preset } from "../../../lib/presets";
import { useUi } from "../../../state/store";

type Set<T> = Dispatch<SetStateAction<T>>;

export interface TaskFieldProps {
  /** Whose box: "new" (the New form) or "tpl" (a template's editor). */
  scope?: string;
  /** The muted words after "Task". */
  hint?: string;
  placeholder?: string;
  prompt: string;
  setPrompt: Set<string>;
  /** The "Insert saved text…" picker — the New form's; a form without it (a
   * template's, whose Manage… could not hand back to it) leaves these out. */
  presetValue?: string;
  setPresetValue?: Set<string>;
  savedPresets?: Preset[];
}

/** The tallest the Task box grows to by itself before it scrolls. */
const TASK_MAX_PX = 220;

/** The Task with saved text inserted: at the caret (over a selection) when
 * the box has had one, else after what is there with a blank line between —
 * it used to REPLACE the Task, and whatever was typed was gone. */
export function insertSaved(
  text: string,
  insert: string,
  caret: { start: number; end: number } | null
): string {
  if (!text.trim()) return insert;
  if (!caret) return text.replace(/\s+$/, "") + "\n\n" + insert;
  const start = Math.max(0, Math.min(caret.start, text.length));
  const end = Math.max(start, Math.min(caret.end, text.length));
  return text.slice(0, start) + insert + text.slice(end);
}

export function TaskField({
  scope = "new",
  hint = "what the agent starts on; blank starts it idle",
  placeholder = "e.g. add a section about setup to the README",
  prompt,
  setPrompt,
  presetValue = "",
  setPresetValue,
  savedPresets = [],
}: TaskFieldProps) {
  // Two rows to start — most tasks are a sentence, and an empty four-row box
  // was a third of the card — growing with what is typed (or inserted, or
  // carried over from page 1) up to TASK_MAX_PX, then scrolling.
  const ref = useRef<HTMLTextAreaElement | null>(null);
  // Where the caret was when the box last had it (null = never touched):
  // picking a saved text moves focus to the select, so it's kept here.
  const caret = useRef<{ start: number; end: number } | null>(null);
  const noteCaret = () => {
    const ta = ref.current;
    if (ta) caret.current = { start: ta.selectionStart, end: ta.selectionEnd };
  };
  useLayoutEffect(() => {
    const ta = ref.current;
    if (!ta) return;
    ta.style.height = "auto";
    const edge = ta.offsetHeight - ta.clientHeight; // the borders
    ta.style.height = Math.min(TASK_MAX_PX, ta.scrollHeight + edge) + "px";
  }, [prompt]);
  return (
    <div id={scope + "-prompt-fold"} className="nf-task">
      <div className="nf-task-head">
        <label htmlFor={scope + "-prompt"}>
          Task <span className="muted">— {hint}</span>
        </label>
        {setPresetValue && (
          <span className="preset-row">
            <select
              id="new-preset"
              aria-label="Insert saved text"
              title="Put one of your saved prompts in the Task box, at the caret (you can edit it after)"
              value={presetValue}
              onChange={(e) => {
                const p = findPreset(e.target.value);
                // A one-shot action: the select goes back to its prompt, so
                // the same text can be inserted twice.
                setPresetValue("");
                if (!p) return;
                setPrompt((cur) => insertSaved(cur, p.prompt, caret.current));
                caret.current = null;
              }}
            >
              <option value="">Insert saved text…</option>
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
              title="Add or remove prompts, or pick one for this Task — you come back here on Close"
              // Not a hand-off that loses the form: Close in Prompts brings
              // this dialog back as it was (newSessionResume).
              onClick={() => useUi.getState().openDialogFor("prompts", "new")}
            >
              Manage…
            </button>
          </span>
        )}
      </div>
      <textarea
        id={scope + "-prompt"}
        ref={ref}
        rows={2}
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onSelect={noteCaret}
        onBlur={noteCaret}
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
          ? "Before any edit, the agent lists the files it will change and why, then waits. Review it " +
            "on the session's Map tab and press Go."
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
            ? "— the agent lists the files it will change and waits for your OK (Map tab)"
            : "— needs a task: the agent lists the files it will change and waits for your OK"}
      </span>
    </label>
  );
}
