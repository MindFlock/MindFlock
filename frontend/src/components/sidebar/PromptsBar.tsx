/** The Prompts bar: your saved prompts on the left, one click from any session.
 *
 * Pick where the text goes ("→" — the focused session by default, any other
 * running session by its rail name, or all of them), then click a prompt: it
 * is pasted into that session's input, NOT sent — press Enter there. Hover a
 * prompt to read it. "Manage" opens the Prompts dialog to add or delete them.
 *
 * Built-ins and your own prompts share one row of chips (yours first: they are
 * the ones you made for a reason). Switched on and off in Customize like every
 * other bar. */

import { useEffect, useMemo, useState } from "react";
import { useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { windowName } from "../../lib/windowName";
import { promptTargets, resolveTarget, runningTitles, ALL_RUNNING } from "../../lib/promptTargets";
import { pastePrompt } from "../../lib/promptPaste";
import { BUILTIN_PRESETS, PRESETS_CHANGED, loadUserPresets, type Preset } from "../../lib/presets";

export function PromptsBar() {
  const focused = useUi((s) => s.focused);
  const railOrder = useUi((s) => s.railOrder);
  const aliases = useUi((s) => s.aliases);
  const openDialogFor = useUi((s) => s.openDialogFor);
  const { data: instances } = useInstances();
  const [picked, setPicked] = useState<string | null>(null);
  const [saved, setSaved] = useState<Preset[]>(() => loadUserPresets());

  // Re-read when another surface edits them (the Prompts dialog, New's
  // inline save, another tab) — they all write the same localStorage key.
  useEffect(() => {
    const reload = () => setSaved(loadUserPresets());
    document.addEventListener(PRESETS_CHANGED, reload);
    window.addEventListener("storage", reload);
    return () => {
      document.removeEventListener(PRESETS_CHANGED, reload);
      window.removeEventListener("storage", reload);
    };
  }, []);

  // `aliases` is read so a rename re-labels the picker without a refetch.
  const running = useMemo(() => runningTitles(instances, railOrder), [instances, railOrder]);
  const options = useMemo(
    () => promptTargets(focused, running, (t) => aliases[t] || windowName(t)),
    [focused, running, aliases]
  );
  const target = resolveTarget(picked, focused, options);
  const prompts = [...saved, ...BUILTIN_PRESETS];

  return (
    <div id="prompts-bar">
      <div className="pb-head">
        <span className="pb-label">Prompts</span>
        <label className="pb-target" title="Where a clicked prompt is pasted">
          <span aria-hidden="true">→</span>
          <select
            id="prompts-bar-target"
            aria-label="Paste into"
            value={target}
            disabled={!options.length}
            onChange={(e) => setPicked(e.target.value || null)}
          >
            {!target && <option value="">no session selected</option>}
            {options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <button
          id="prompts-bar-manage"
          type="button"
          className="as-toggle"
          title="Add, read or delete saved prompts"
          onClick={() => openDialogFor("prompts")}
        >
          Manage
        </button>
      </div>
      <div className="pb-chips">
        {prompts.map((p, i) => (
          <button
            key={(i < saved.length ? "u:" : "b:") + p.name}
            type="button"
            className="pb-chip"
            title={
              (target === ALL_RUNNING ? "Paste into every running session" : "Paste into the chosen session") +
              " (Enter there sends it):\n\n" +
              p.prompt
            }
            onClick={() => void pastePrompt(target, running, p.prompt)}
          >
            {p.name}
          </button>
        ))}
      </div>
    </div>
  );
}
