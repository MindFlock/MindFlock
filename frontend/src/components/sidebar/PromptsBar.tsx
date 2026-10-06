/** The Prompts bar: your saved prompts, one click from the session you are
 * in, in ONE short line of the sidebar however many prompts you keep.
 *
 * "Paste ▾" opens the menu (PromptsMenu): yours first, then the built-ins,
 * with "Manage prompts…" (the Prompts dialog) last. A pick is pasted into the
 * FOCUSED session's input, NOT sent, and the keyboard moves to that session's
 * terminal so Enter sends it. There is no target picker here: the prompt goes
 * where you are working. Pasting into another session, or into all running
 * ones, is the Prompts dialog's job.
 *
 * Switched on and off in Customize like every other bar. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { windowName } from "../../lib/windowName";
import { pastePrompt } from "../../lib/promptPaste";
import { focusTerm } from "../../lib/terminals";
import { BUILTIN_PRESETS, PRESETS_CHANGED, loadUserPresets, type Preset } from "../../lib/presets";
import { PromptsMenu } from "./PromptsMenu";

export function PromptsBar() {
  const focused = useUi((s) => s.focused);
  const alias = useUi((s) => (focused ? s.aliases[focused] : undefined));
  const openDialogFor = useUi((s) => s.openDialogFor);
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

  const target = focused || "";
  const targetLabel = focused ? alias || windowName(focused) : "";

  const paste = async (p: Preset) => {
    if (await pastePrompt(target, [], p.prompt)) focusTerm(target);
  };

  return (
    <div id="prompts-bar">
      <span className="pb-label">Prompts</span>
      <button
        id="prompts-bar-menu"
        ref={menuBtn}
        type="button"
        className={"as-toggle" + (menuOpen ? " active" : "")}
        title={
          targetLabel
            ? `Paste a saved prompt into ${targetLabel} (or manage them)`
            : "Select a session, then paste a saved prompt into it"
        }
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
          onPick={(p) => void paste(p)}
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
