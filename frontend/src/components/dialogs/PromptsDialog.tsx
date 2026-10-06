/** The Prompts tab of Customize (port of initPromptsTab, section 23): saved
 * TEXT, pasted into sessions that are already running (POST /send
 * submit:false — nothing is sent until you press Enter there). The target
 * picker chooses where: the focused session (default), any other running
 * session, or all of them. Add/delete saved prompts — the same store New's
 * "Saved prompt…" picker reads. Templates (a saved New-session setup) only
 * ever START a session; pasting into running ones is this panel's job alone.
 * The dialog shell (Close, Esc, backdrop) is Customize's; this panel only
 * claims Esc while its preview is open. */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { instApi } from "../../api/client";
import { useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import { windowName } from "../../lib/windowName";
import {
  ALL_RUNNING,
  pasteIntoAll,
  pastedAllToast,
  promptTargets,
  resolveTarget,
  runningTitles,
} from "../../lib/promptTargets";
import { BUILTIN_PRESETS, loadUserPresets, saveUserPresets, type Preset } from "../../lib/presets";

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
  // which Customize's own Esc handler (on window, so it runs after this one)
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

  // One paste at a time: a second click while "All" is still fanning out
  // would paste the text twice into every session.
  const busy = useRef(false);
  // Close Customize only if it is still the open dialog: the paste awaits the
  // server (once per session for All), and a dialog opened meanwhile — the
  // user pressed Esc and moved on — must not be the one this closes.
  const closeIfStillOpen = () => {
    if (useUi.getState().openDialog === "prompts") closeDialog();
  };

  const pastePrompt = async (prompt: string) => {
    if (!target) {
      toast("Choose a session to paste into first, then click a prompt");
      return;
    }
    if (busy.current) return;
    busy.current = true;
    // dialog_safe: the server re-checks the agent live and refuses (409,
    // nothing typed) when it sits on a permission/limit prompt, where typed
    // text would ANSWER the dialog — the target may be a session you can't
    // see. Same contract as every other UI paste (lib/flockActions.ts).
    const send = (t: string) =>
      instApi(t, "/send", { json: { text: prompt, submit: false, dialog_safe: true } });
    try {
      if (target === ALL_RUNNING) {
        const titles = running.slice();
        const { ok, failed } = await pasteIntoAll(titles, send);
        if (failed.length) {
          errorPop(
            `Couldn't paste into ${failed.length} of ${titles.length} sessions`,
            failed.map((f) => windowName(f.title) + ": " + f.error).join(" · ")
          );
        }
        if (!ok.length) return;
        toast(pastedAllToast(ok.length));
        setPop(null);
        closeIfStillOpen();
        return;
      }
      try {
        await send(target);
        toast("Pasted into " + windowName(target));
        setPop(null);
        closeIfStillOpen();
      } catch (err) {
        toast("Paste failed: " + ((err as Error).message || ""));
      }
    } finally {
      busy.current = false;
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
        Choose where it goes, then click a prompt to paste it there — nothing is sent until you
        press Enter in that session (in each, for all running sessions). Saved prompts also show
        in New → Saved prompt.
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
