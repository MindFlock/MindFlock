/** Prompt library dialog (port of initPromptsTab, section 23): click a
 * prompt to paste it into the FOCUSED session (POST /send submit:false);
 * add/delete saved prompts — same store as the New-session preset picker. */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { instApi } from "../../api/client";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { BUILTIN_PRESETS, loadUserPresets, saveUserPresets, type Preset } from "../../lib/presets";

export function PromptsDialog() {
  const open = useUi((s) => s.openDialog === "prompts");
  const closeDialog = useUi((s) => s.closeDialog);
  const focused = useUi((s) => s.focused);
  const [saved, setSaved] = useState<Preset[]>([]);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [pop, setPop] = useState<{ anchor: DOMRect; text: string; key: string } | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (open) {
      setSaved(loadUserPresets());
      setPop(null);
    }
  }, [open]);

  // The fixed preview popover anchors to a point — dismiss on scroll/resize.
  useEffect(() => {
    if (!open) return;
    const closePop = () => setPop(null);
    const list = listRef.current;
    list?.addEventListener("scroll", closePop);
    window.addEventListener("resize", closePop);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        // First Esc closes the popover, not the dialog.
        setPop((p) => {
          if (p) return null;
          closeDialog();
          return null;
        });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      list?.removeEventListener("scroll", closePop);
      window.removeEventListener("resize", closePop);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, closeDialog]);

  if (!open) return null;

  const pastePrompt = async (prompt: string) => {
    if (!focused) {
      toast("Select a session first, then click a prompt");
      return;
    }
    try {
      await instApi(focused, "/send", { json: { text: prompt, submit: false } });
      toast("Pasted into " + focused);
      setPop(null);
      closeDialog();
    } catch (err) {
      toast("Paste failed: " + ((err as Error).message || ""));
    }
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
    const list = loadUserPresets().filter((p) => p.name !== n);
    list.push({ name: n, prompt: t });
    saveUserPresets(list);
    setSaved(list);
    setName("");
    setText("");
    toast(`Added prompt “${n}”`);
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
                  title="Paste into the selected session"
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
                    setPop((cur) =>
                      cur?.key === key
                        ? null
                        : {
                            anchor: (e.currentTarget as HTMLElement).getBoundingClientRect(),
                            text: p.prompt,
                            key,
                          }
                    );
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
      id="prompts-dialog"
      className="modal"
      onClick={(e) => {
        if (pop) setPop(null);
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="prompts-panel">
        <div className="ws-head">
          <h2>Prompts</h2>
          <span id="prompts-target" className={"muted" + (!focused ? " prompts-notarget" : "")}>
            {focused ? "→ " + focused : "no session selected"}
          </span>
          <button type="button" id="prompts-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <p className="prompts-hint">
          Click a prompt to paste it into the selected session. Saved prompts also appear in the
          New-session preset picker.
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
