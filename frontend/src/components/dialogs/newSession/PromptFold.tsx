/** The New form's Prompt fold: saved prompts (one store with Customize →
 * Prompts), the prompt itself, and Plan first. */

import { useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import {
  BUILTIN_PRESETS,
  findPreset,
  loadUserPresets,
  saveUserPresets,
  upsertUserPreset,
  type Preset,
} from "../../../lib/presets";
import { toast } from "../../../lib/toast";
import { useUi } from "../../../state/store";

type Set<T> = Dispatch<SetStateAction<T>>;

export interface PromptFoldProps {
  prompt: string;
  setPrompt: Set<string>;
  promptOpen: boolean;
  setPromptOpen: Set<boolean>;
  /** Set when a plan opened the fold, so its toggle does not scroll. */
  foldOpenedByPlan: MutableRefObject<boolean>;
  presetValue: string;
  setPresetValue: Set<string>;
  savedPresets: Preset[];
  setSavedPresets: Set<Preset[]>;
  presetName: string | null;
  setPresetName: Set<string | null>;
  planOk: boolean;
  planFirst: boolean;
  setPlanFirst: Set<boolean>;
}

export function PromptFold({
  prompt,
  setPrompt,
  promptOpen,
  setPromptOpen,
  foldOpenedByPlan,
  presetValue,
  setPresetValue,
  savedPresets,
  setSavedPresets,
  presetName,
  setPresetName,
  planOk,
  planFirst,
  setPlanFirst,
}: PromptFoldProps) {
  const promptRef = useRef<HTMLDetailsElement | null>(null);

  /** Save the prompt under the name typed in the inline row. Same store as
   * Customize → Prompts' list (lib/presets), so a prompt saved here shows there
   * and the other way round. Refused (with the row left open) rather than
   * saving an empty prompt or a nameless one. */
  const savePreset = () => {
    const text = prompt.trim();
    const name = (presetName || "").trim();
    if (!text || !name) return;
    // Same name ignoring case = the same prompt (the server's rule).
    const { replaced } = upsertUserPreset(name, text);
    setSavedPresets(loadUserPresets());
    setPresetValue("u:" + name);
    setPresetName(null);
    toast(
      replaced
        ? `Saved prompt “${name}” — it replaced “${replaced}” (names ignore case)`
        : `Saved prompt “${name}”`
    );
  };

  return (
    <details
      id="new-prompt-fold"
      className="nf-advanced"
      ref={promptRef}
      open={promptOpen}
      onToggle={(e) => {
        const open = (e.target as HTMLDetailsElement).open;
        setPromptOpen(open);
        // Scroll to it only when a PERSON opened it. A plan opens this
        // fold to show the prompt it wrote, and scrolling to the bottom
        // of the form on arrival buries the two fields most likely to
        // be wrong — see foldOpenedByPlan.
        if (foldOpenedByPlan.current) {
          foldOpenedByPlan.current = false;
          return;
        }
        if (open)
          promptRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
      }}
    >
      <summary>
        Prompt <span className="muted">— sent to the agent at launch</span>
      </summary>
      <div className="nf-advanced-body">
        <label>
          <span className="preset-row">
            <select
              id="new-preset"
              title="Saved prompts — pick one to fill the prompt below (editable after)"
              value={presetValue}
              onChange={(e) => {
                setPresetValue(e.target.value);
                const p = findPreset(e.target.value);
                if (p) setPrompt(p.prompt);
              }}
            >
              <option value="">Saved prompt…</option>
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
              id="preset-save"
              title="Save the prompt below so you can pick it again"
              aria-expanded={presetName !== null}
              onClick={() => setPresetName((cur) => (cur === null ? "" : null))}
            >
              Save…
            </button>
            {presetValue.startsWith("u:") && (
              <button
                type="button"
                id="preset-del"
                title="Delete this saved prompt"
                onClick={() => {
                  const p = findPreset(presetValue);
                  if (!p) return;
                  const list = loadUserPresets().filter((q) => q.name !== p.name);
                  saveUserPresets(list);
                  setSavedPresets(list);
                  setPresetValue("");
                }}
              >
                ✕
              </button>
            )}
            {/* Customize → Prompts: rename, preview and paste live
                there; this picker only picks and saves. */}
            <button
              type="button"
              id="preset-manage"
              className="linklike"
              title="Your saved prompts, in Customize → Prompts"
              onClick={() => useUi.getState().openDialogFor("prompts")}
            >
              Manage…
            </button>
          </span>
          {presetName !== null && (
            <span className="preset-name-row">
              <input
                id="preset-name"
                type="text"
                autoFocus
                autoComplete="off"
                spellCheck={false}
                placeholder="Name this prompt"
                aria-label="Name for the saved prompt"
                value={presetName}
                onChange={(e) => setPresetName(e.target.value)}
                onKeyDown={(e) => {
                  // Kept to this row: Enter must not submit the form
                  // (that creates the session) and Escape must not
                  // reach the dialog's handler (that closes it).
                  e.stopPropagation();
                  if (e.key === "Enter") {
                    e.preventDefault();
                    savePreset();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    setPresetName(null);
                  }
                }}
              />
              <button
                type="button"
                id="preset-name-save"
                disabled={!presetName.trim() || !prompt.trim()}
                title={prompt.trim() ? "Save it" : "Type a prompt below first"}
                onClick={savePreset}
              >
                Save
              </button>
              <button type="button" id="preset-name-cancel" onClick={() => setPresetName(null)}>
                Cancel
              </button>
              {!prompt.trim() && <span className="muted">Type a prompt below first.</span>}
            </span>
          )}
          <textarea
            id="new-prompt"
            rows={2}
            autoComplete="off"
            spellCheck={false}
            placeholder="What should the agent do first? Leave blank if you don’t want to kick anything off just yet."
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </label>
        <label
          className={"check" + (planOk ? "" : " disabled")}
          id="new-plan-first-row"
          title={
            planOk
              ? "The agent first lists every file it intends to create, modify or delete, with a one-line " +
                "intent for each, then waits for your go-ahead. Open the session's Map tab to see that plan " +
                "and its blast radius, red-zone anything it shouldn't touch, and press Go."
              : "Only a CLI that can declare a plan gets the Map's plan review and Go button — pick Claude " +
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
              ? "(needs a CLI with plan support — Claude)"
              : prompt.trim()
                ? "(list files + intent, then wait for Go on the Map tab)"
                : "(takes effect with a prompt)"}
          </span>
        </label>
      </div>
    </details>
  );
}
