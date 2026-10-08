/** The Prompts dialog (port of initPromptsTab, section 23): where saved
 * prompts are managed — read, add, delete — and, like the sidebar Prompts bar,
 * pasted into a running session (POST /send submit:false — nothing is sent
 * until you press Enter there). The daily door is the bar; this is "Manage".
 * The target picker chooses where: the focused session (default), any other
 * running session, or all of them. Same store New's "Saved prompt…" reads.
 * Templates (a saved New-session setup) only ever START a session.
 * Esc closes the ⋮ preview first, then the dialog. */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { windowName } from "../../lib/windowName";
import { ALL_RUNNING, promptTargets, resolveTarget, runningTitles } from "../../lib/promptTargets";
import { pastePrompt as pasteInto } from "../../lib/promptPaste";
import {
  BUILTIN_PRESETS,
  loadUserPresets,
  saveUserPresets,
  upsertUserPreset,
  type Preset,
} from "../../lib/presets";

export function PromptsPanel() {
  const closeDialog = useUi((s) => s.closeDialog);
  const focused = useUi((s) => s.focused);
  const railOrder = useUi((s) => s.railOrder);
  const aliases = useUi((s) => s.aliases);
  const { data: instances } = useInstances();
  // null = "follow the focused session"; a pick sticks while it is offered.
  const [picked, setPicked] = useState<string | null>(null);
  const [saved, setSaved] = useState<Preset[]>([]);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [pop, setPop] = useState<{ anchor: DOMRect; text: string; key: string } | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setSaved(loadUserPresets());
    setPop(null);
  }, []);

  // The fixed preview popover anchors to a point — dismiss on scroll/resize.
  useEffect(() => {
    const closePop = () => setPop(null);
    const list = listRef.current;
    list?.addEventListener("scroll", closePop);
    window.addEventListener("resize", closePop);
    return () => {
      list?.removeEventListener("scroll", closePop);
      window.removeEventListener("resize", closePop);
    };
  }, []);

  // First Esc closes the preview, not the dialog: claim it (preventDefault),
  // which the dialog's own Esc handler (on window, so it runs after this one)
  // reads as "handled".
  useEffect(() => {
    if (!pop) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setPop(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pop]);

  // The rail's name for each row (rename, else ticket/PR label, else title).
  // `aliases` is read so a rename re-labels the picker without a refetch.
  const running = useMemo(() => runningTitles(instances, railOrder), [instances, railOrder]);
  const options = useMemo(
    () => promptTargets(focused, running, (t) => aliases[t] || windowName(t)),
    [focused, running, aliases]
  );
  const target = resolveTarget(picked, focused, options);

  // Close only if this is still the open dialog: the paste awaits the server
  // (once per session for All), and a dialog opened meanwhile — the user
  // pressed Esc and moved on — must not be the one this closes.
  const pastePrompt = async (prompt: string) => {
    if (!(await pasteInto(target, running, prompt))) return;
    setPop(null);
    if (useUi.getState().openDialog === "prompts") closeDialog();
  };

  const addPrompt = () => {
    const n = name.trim();
    const t = text.trim();
    if (!n) {
      toast("Give the prompt a name");
      return;
    }
    if (!t) {
      toast("Enter the prompt text");
      return;
    }
    // A name is the same prompt ignoring case (the server's rule): replace
    // it, and say so when the old one was spelled differently.
    const { replaced } = upsertUserPreset(n, t);
    setSaved(loadUserPresets());
    setName("");
    setText("");
    toast(
      replaced
        ? `Saved prompt “${n}” — it replaced “${replaced}” (names ignore case)`
        : `Added prompt “${n}”`
    );
  };

  const section = (label: string, items: Preset[], deletable: boolean, kind: string) =>
    items.length ? (
      <div key={label}>
        <div className="prompts-group-label">{label}</div>
        {items.map((p) => {
          const key = kind + ":" + p.name;
          return (
            <div className="prompt-card" key={key}>
              <div className="prompt-card-row">
                <button
                  type="button"
                  className="prompt-card-main"
                  title={
                    target === ALL_RUNNING
                      ? "Paste into every running session"
                      : "Paste into the chosen session"
                  }
                  onClick={() => pastePrompt(p.prompt)}
                >
                  <span className="prompt-card-name">{p.name}</span>
                </button>
                <button
                  type="button"
                  className={"prompt-card-expand" + (pop?.key === key ? " open" : "")}
                  title="Show the full prompt"
                  aria-expanded={pop?.key === key}
                  onClick={(e) => {
                    e.stopPropagation();
                    // Read the rect NOW: React clears e.currentTarget once the
                    // handler returns, and the setPop updater runs later — a
                    // read in there threw and blanked the app.
                    const anchor = e.currentTarget.getBoundingClientRect();
                    setPop((cur) => (cur?.key === key ? null : { anchor, text: p.prompt, key }));
                  }}
                >
                  ⋮
                </button>
                {deletable && (
                  <button
                    type="button"
                    className="prompt-card-del"
                    title="Delete this saved prompt"
                    onClick={(e) => {
                      e.stopPropagation();
                      setPop(null);
                      const list = loadUserPresets().filter((q) => q.name !== p.name);
                      saveUserPresets(list);
                      setSaved(list);
                    }}
                  >
                    ✕
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    ) : null;

  // Right-align the preview's edge to the ⋮, clamped to the viewport; flip
  // above the row if it would spill past the bottom (approximated by height cap).
  const popStyle = pop
    ? (() => {
        const w = Math.min(380, window.innerWidth - 24);
        const left = Math.max(12, Math.min(pop.anchor.right - w, window.innerWidth - w - 12));
        return { width: w + "px", left: left + "px", top: pop.anchor.bottom + 6 + "px" };
      })()
    : undefined;

  return (
    <div
      id="prompts-panel"
      onClick={() => {
        if (pop) setPop(null);
      }}
    >
      <div className="cz-tab-head">
        <label className="prompts-target-wrap" onClick={(e) => e.stopPropagation()}>
          <span className="muted">Paste into</span>
          <select
            id="prompts-target"
            className={!target ? "prompts-notarget" : ""}
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
      </div>
      <p className="prompts-hint">
        Click a prompt to paste it into the chosen session — nothing is sent until you press Enter
        there. The same prompts sit in the sidebar's Prompts bar and in New → Saved prompt.
      </p>
      <div id="prompts-list" ref={listRef}>
        {section("Built-in", BUILTIN_PRESETS, false, "b")}
        {section("Saved", saved, true, "u")}
      </div>
      <div className="prompts-add">
        <input
          type="text"
          id="prompts-add-name"
          autoComplete="off"
          spellCheck={false}
          placeholder="New prompt name…"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <textarea
          id="prompts-add-text"
          rows={3}
          placeholder="Prompt text…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
              e.preventDefault();
              addPrompt();
            }
          }}
        />
        <button type="button" id="prompts-add-btn" onClick={addPrompt}>
          Add prompt
        </button>
      </div>
      {pop &&
        createPortal(
          <div className="prompt-pop" style={popStyle} onClick={(e) => e.stopPropagation()}>
            {pop.text}
          </div>,
          document.body
        )}
    </div>
  );
}

/** The modal shell: header, Close, Esc and backdrop. Opened by the Prompts
 * bar's "Manage", New's "Manage…" and the palette. */
export function PromptsDialog() {
  const open = useUi((s) => s.openDialog === "prompts");
  const closeDialog = useUi((s) => s.closeDialog);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      closeDialog();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!open) return null;
  return (
    <div
      id="prompts-dialog"
      className="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="prompts-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="prompts-dialog-panel">
        <div className="ws-head">
          <h2 id="prompts-title">Prompts</h2>
          <span className="ik-subtitle">Saved text you paste into sessions</span>
          <button type="button" id="prompts-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <PromptsPanel />
      </div>
    </div>
  );
}
