/** The Prompts bar's menu: every saved prompt, opened from the bar's
 * "Paste ▾" button so the bar itself stays one line however many prompts
 * there are.
 *
 * Yours first, then the built-ins, each with the start of its text under the
 * name (hover for all of it); "Manage prompts…" last. Picking one hands it
 * back to the bar, which pastes it — this menu never touches a session.
 * Body-mounted and fixed (the sidebar scrolls and would clip it), keyboard
 * first like the ⏩ picker it borrows its look from: arrows, Enter, Esc. Its
 * id is in the keymap's MODAL_DOM_IDS, so a Delete or Ctrl+W pressed at it can
 * never end the session behind it. */

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Preset } from "../../lib/presets";

type Entry = { kind: "prompt"; preset: Preset; mine: boolean } | { kind: "manage" };

export function PromptsMenu({
  anchor,
  mine,
  builtins,
  targetLabel,
  onPick,
  onManage,
  onClose,
}: {
  anchor: HTMLElement;
  mine: Preset[];
  builtins: Preset[];
  /** Where a pick lands, for the head ("→ flaky-rollup"); "" when nowhere. */
  targetLabel: string;
  onPick(p: Preset): void;
  onManage(): void;
  /** `refocus`: give the keyboard back to the button (Esc). */
  onClose(refocus: boolean): void;
}) {
  const entries: Entry[] = [
    ...mine.map((preset) => ({ kind: "prompt" as const, preset, mine: true })),
    ...builtins.map((preset) => ({ kind: "prompt" as const, preset, mine: false })),
    { kind: "manage" as const },
  ];
  const [sel, setSel] = useState(0);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const uid = useId();
  const itemId = (i: number) => uid + "-item-" + i;

  useEffect(() => {
    menuRef.current?.focus({ preventScroll: true });
  }, []);

  // Under the button, left edges aligned (the bar is on the left of the
  // window); upward when there is more room above; clamped to the window.
  useLayoutEffect(() => {
    const m = menuRef.current;
    if (!m) return;
    const r = anchor.getBoundingClientRect();
    m.style.maxHeight = "none";
    const natural = m.scrollHeight;
    const below = window.innerHeight - r.bottom - 18;
    const above = r.top - 18;
    if (natural > below && above > below) {
      const h = Math.min(natural, above);
      m.style.top = Math.max(8, Math.round(r.top - 6 - h)) + "px";
      m.style.maxHeight = h + "px";
    } else {
      const top = Math.round(r.bottom + 6);
      m.style.top = top + "px";
      m.style.maxHeight = Math.max(160, window.innerHeight - top - 12) + "px";
    }
    const left = Math.min(r.left, window.innerWidth - m.offsetWidth - 8);
    m.style.left = Math.max(8, Math.round(left)) + "px";
  }, [anchor]);

  // Outside click, resize and a scroll anywhere but inside the menu close it.
  // The button is exempt: its own click toggles.
  useEffect(() => {
    const inside = (t: EventTarget | null) =>
      t instanceof Node && (!!menuRef.current?.contains(t) || anchor.contains(t));
    const onDown = (e: MouseEvent) => {
      if (!inside(e.target)) onCloseRef.current(false);
    };
    const onScroll = (e: Event) => {
      if (!inside(e.target)) onCloseRef.current(false);
    };
    const onResize = () => onCloseRef.current(false);
    document.addEventListener("mousedown", onDown, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [anchor]);

  // Keep the highlighted row in view while arrowing through a long list.
  useEffect(() => {
    document.getElementById(uid + "-item-" + sel)?.scrollIntoView({ block: "nearest" });
  }, [sel, uid]);

  const activate = (e: Entry | undefined) => {
    if (!e) return;
    // Close first: the answer is the paste (or the dialog), not this menu.
    onCloseRef.current(false);
    if (e.kind === "manage") onManage();
    else onPick(e.preset);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    const handled = () => {
      e.preventDefault();
      e.stopPropagation();
    };
    if (k === "ArrowDown") {
      handled();
      setSel((i) => (i + 1) % entries.length);
    } else if (k === "ArrowUp") {
      handled();
      setSel((i) => (i - 1 + entries.length) % entries.length);
    } else if (k === "Home") {
      handled();
      setSel(0);
    } else if (k === "End") {
      handled();
      setSel(entries.length - 1);
    } else if (k === "Enter" || k === " ") {
      handled();
      activate(entries[sel]);
    } else if (k === "Escape" || k === "Tab") {
      handled();
      onCloseRef.current(true);
    }
  };

  const row = (e: Entry, i: number) => (
    <div
      key={e.kind === "manage" ? "manage" : (e.mine ? "u:" : "b:") + e.preset.name}
      id={itemId(i)}
      className={"pb-item" + (i === sel ? " sel" : "") + (e.kind === "manage" ? " pm-manage" : "")}
      role="menuitem"
      tabIndex={-1}
      title={e.kind === "prompt" ? e.preset.prompt : "Add, read or delete saved prompts"}
      onMouseMove={() => setSel(i)}
      onClick={() => activate(e)}
    >
      <span className="pb-name">{e.kind === "manage" ? "Manage prompts…" : e.preset.name}</span>
      {e.kind === "prompt" && <span className="pb-desc">{e.preset.prompt}</span>}
    </div>
  );

  const builtinStart = mine.length;
  const manageAt = entries.length - 1;
  return createPortal(
    <div
      id="prompts-menu"
      className="pb-menu pm-menu"
      role="menu"
      aria-label="Paste a saved prompt"
      tabIndex={-1}
      ref={menuRef}
      aria-activedescendant={itemId(sel)}
      style={{ top: 0, left: 0 }}
      onKeyDown={onKeyDown}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="pb-head">
        <b>Paste a prompt</b>
        <span className="muted">{targetLabel ? "→ " + targetLabel : "— select a session first"}</span>
      </div>
      {mine.length > 0 && <div className="pb-sec">Yours</div>}
      {entries.slice(0, builtinStart).map((e, i) => row(e, i))}
      {builtins.length > 0 && <div className="pb-sec">Built-in</div>}
      {entries.slice(builtinStart, manageAt).map((e, i) => row(e, builtinStart + i))}
      <div className="pb-sep" />
      {row(entries[manageAt], manageAt)}
      <div className="pb-foot">Pasted into the session you are in, not sent — press Enter to send it.</div>
    </div>,
    document.body
  );
}
