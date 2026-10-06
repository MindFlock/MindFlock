/** The Prompts bar: your saved prompts, one click from any session, in ONE
 * line of the sidebar however many prompts you keep.
 *
 * Pick where the text goes ("→" — the focused session by default, any other
 * running session by its rail name, or all of them), then open "Paste ▾" and
 * pick a prompt: it is pasted into that session's input, NOT sent — press
 * Enter there. The menu (PromptsMenu) lists yours first, then the built-ins,
 * with "Manage prompts…" (the Prompts dialog) last.
 *
 * The prompts used to sit under the head as wrapping chips; prompt names are
 * phrases, so in a 260px sidebar they stacked one per line and the bar ate
 * ~150px of the session list. Switched on and off in Customize like every
 * other bar. */

import { useEffect, useMemo, useRef, useState } from "react";
import { useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { windowName } from "../../lib/windowName";
import { promptTargets, resolveTarget, runningTitles } from "../../lib/promptTargets";
import { pastePrompt } from "../../lib/promptPaste";
import { BUILTIN_PRESETS, PRESETS_CHANGED, loadUserPresets, type Preset } from "../../lib/presets";
import { PromptsMenu } from "./PromptsMenu";

export function PromptsBar() {
  const focused = useUi((s) => s.focused);
  const railOrder = useUi((s) => s.railOrder);
  const aliases = useUi((s) => s.aliases);
  const openDialogFor = useUi((s) => s.openDialogFor);
  const { data: instances } = useInstances();
  const [picked, setPicked] = useState<string | null>(null);
  const [saved, setSaved] = useState<Preset[]>(() => loadUserPresets());
  const [menuOpen, setMenuOpen] = useState(false);
  const menuBtn = useRef<HTMLButtonElement | null>(null);

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
  const targetLabel = options.find((o) => o.value === target)?.label || "";

  return (
    <div id="prompts-bar">
      <span className="pb-label">Prompts</span>
      <label className="pb-target" title="Where a picked prompt is pasted">
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
        id="prompts-bar-menu"
        ref={menuBtn}
        type="button"
        className={"as-toggle" + (menuOpen ? " active" : "")}
        title="Pick a saved prompt to paste (or manage them)"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((o) => !o)}
      >
        Paste ▾
      </button>
      {menuOpen && menuBtn.current && (
        <PromptsMenu
          anchor={menuBtn.current}
          mine={saved}
          builtins={BUILTIN_PRESETS}
          targetLabel={targetLabel}
          onPick={(p) => void pastePrompt(target, running, p.prompt)}
          onManage={() => openDialogFor("prompts")}
          onClose={(refocus) => {
            setMenuOpen(false);
            if (refocus) menuBtn.current?.focus();
          }}
        />
      )}
    </div>
  );
}
