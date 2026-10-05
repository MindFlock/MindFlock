/** "Work with other sessions" — the menu under a pane's fork-icon button (and
 * Ctrl+K F). Lists the session's playbooks as the server judges them (GET
 * /api/playbooks?title=…): Split across workers, Ask a session › (a session
 * picker), and — once the session has workers — a "<name>'s workers · N"
 * section with Check on workers and Wrap up workers, then Message…, which
 * opens the Thread composer as the user rather than pasting anything.
 *
 * Every playbook here is a PASTE (lib/playbooks.pastePlaybook): its prompt is
 * typed into the agent's input box and the user presses Enter. The footer says
 * so in as many words, because a menu item that looks like a command and then
 * doesn't run is the one way this could confuse.
 *
 * Body-mounted and fixed (the pane head scrolls sideways and would clip it),
 * keyboard-first: arrows, Enter, Esc, and each item's letter. The menu keeps
 * the focus and names the highlighted item with `aria-activedescendant`
 * (each item is a focusable `menuitem` with an id), so a screen reader
 * follows the arrows. Its id is in
 * the keymap's MODAL_DOM_IDS, so a Delete or Ctrl+W pressed while it is open
 * can never end the session behind it. */

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Playbook } from "../../api/types";
import { useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import {
  askTargets,
  fetchPlaybooks,
  letterOf,
  liveChildren,
  menuModel,
  pastePlaybook,
  type AskTarget,
} from "../../lib/playbooks";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";

/** The last list each session's menu showed, so a reopen draws at once and
 * refreshes underneath rather than flashing "Loading…" every time. */
const lastList = new Map<string, Playbook[]>();

type Entry = { kind: "playbook"; pb: Playbook } | { kind: "message" };

export function PlaybookMenu({
  title,
  anchor,
  initialSub,
  onClose,
}: {
  title: string;
  anchor: HTMLElement;
  initialSub?: "ask" | null;
  /** `refocus`: give the keyboard back to the pane's terminal (Esc). */
  onClose(refocus: boolean): void;
}) {
  const { data: rows } = useInstances();
  const railOrder = useUi((s) => s.railOrder);
  const name = useUi((s) => s.aliases[title]) || title;
  const [list, setList] = useState<Playbook[] | null>(lastList.get(title) ?? null);
  const [err, setErr] = useState("");
  const [sel, setSel] = useState(0);
  const [askOpen, setAskOpen] = useState(false);
  const [askSel, setAskSel] = useState(0);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const subRef = useRef<HTMLDivElement | null>(null);
  const askItemRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const uid = useId();
  const itemId = (i: number) => uid + "-item-" + i;
  const sessId = (i: number) => uid + "-sess-" + i;

  useEffect(() => {
    let live = true;
    fetchPlaybooks(title)
      .then((l) => {
        if (!live) return;
        lastList.set(title, l);
        setList(l);
        setErr("");
      })
      .catch((e) => {
        if (live) setErr(errMsg(e));
      });
    return () => {
      live = false;
    };
  }, [title]);

  const children = useMemo(() => liveChildren(title, rows || []), [title, rows]);
  const model = useMemo(() => menuModel(list || [], children), [list, children]);
  const entries = useMemo<Entry[]>(
    () => [
      ...model.general.map((pb) => ({ kind: "playbook" as const, pb })),
      ...(model.workers?.items || []).map((pb) => ({ kind: "playbook" as const, pb })),
      { kind: "message" as const },
    ],
    [model]
  );
  const askPb = (list || []).find((p) => p.args.some((a) => a.kind === "session" && a.required));
  const targets = useMemo<AskTarget[]>(
    () => askTargets(title, rows || [], railOrder, displayName),
    [title, rows, railOrder]
  );

  // Ctrl+K F → "Ask a session…" from the palette lands straight in the picker
  // once the list is in and the Ask entry exists.
  useEffect(() => {
    if (initialSub !== "ask" || !askPb || !askPb.available) return;
    const i = entries.findIndex((e) => e.kind === "playbook" && e.pb.id === askPb.id);
    if (i >= 0) setSel(i);
    setAskOpen(true);
    // Once: the picker is a place the user can back out of.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSub, !!askPb]);

  // The keyboard belongs to the menu while it is open — arrows and letters
  // pressed next must not go on into the agent's terminal.
  useEffect(() => {
    menuRef.current?.focus({ preventScroll: true });
  }, []);

  // Under the button, right edges aligned; clamped to the window, and taller
  // than the room below it scrolls rather than running off the screen.
  useLayoutEffect(() => {
    const m = menuRef.current;
    if (!m) return;
    const r = anchor.getBoundingClientRect();
    const top = Math.round(r.bottom + 6);
    m.style.top = top + "px";
    m.style.maxHeight = Math.max(160, window.innerHeight - top - 12) + "px";
    let left = r.right - m.offsetWidth + 10;
    left = Math.min(left, window.innerWidth - m.offsetWidth - 8);
    m.style.left = Math.max(8, left) + "px";
    const s = subRef.current;
    const item = askItemRef.current;
    if (s && item) {
      const mr = m.getBoundingClientRect();
      const ir = item.getBoundingClientRect();
      let sl = mr.right + 4;
      if (sl + s.offsetWidth > window.innerWidth - 8) sl = mr.left - s.offsetWidth - 4;
      s.style.left = Math.max(8, sl) + "px";
      const st = Math.min(ir.top - 6, window.innerHeight - s.offsetHeight - 8);
      s.style.top = Math.max(8, st) + "px";
    }
  });

  // Outside click, resize and a scroll anywhere but inside the menu close it.
  // The anchor is exempt: its own click handler toggles.
  useEffect(() => {
    const inside = (t: EventTarget | null) =>
      t instanceof Node &&
      (!!menuRef.current?.contains(t) || !!subRef.current?.contains(t) || anchor.contains(t));
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

  const close = (refocus = false) => onCloseRef.current(refocus);

  const activate = (e: Entry | undefined) => {
    if (!e) return;
    if (e.kind === "message") {
      useUi.getState().threadOpen(title, { composeTo: title });
      close();
      return;
    }
    const pb = e.pb;
    if (!pb.available) {
      toast(pb.disabled_reason || "Not available right now", { duration: 5000 });
      return;
    }
    if (pb === askPb) {
      setAskOpen(true);
      setAskSel(0);
      return;
    }
    close();
    void pastePlaybook(title, pb);
  };

  const ask = (t: AskTarget | undefined) => {
    if (!t || !askPb) return;
    close();
    void pastePlaybook(title, askPb, { session: t.title });
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    const handled = () => {
      e.preventDefault();
      e.stopPropagation();
    };
    if (askOpen) {
      if (k === "ArrowDown") {
        handled();
        setAskSel((i) => Math.min(targets.length - 1, i + 1));
      } else if (k === "ArrowUp") {
        handled();
        setAskSel((i) => Math.max(0, i - 1));
      } else if (k === "Enter" || k === "ArrowRight") {
        handled();
        ask(targets[askSel]);
      } else if (k === "Escape" || k === "ArrowLeft") {
        handled();
        setAskOpen(false);
      } else if (/^[1-9]$/.test(k)) {
        // The rail's own numbers pick a session, as Alt+N would focus it.
        const t = targets.find((x) => x.slot === k);
        if (t) {
          handled();
          ask(t);
        }
      }
      return;
    }
    if (k === "ArrowDown") {
      handled();
      setSel((i) => (i + 1) % entries.length);
    } else if (k === "ArrowUp") {
      handled();
      setSel((i) => (i - 1 + entries.length) % entries.length);
    } else if (k === "Enter") {
      handled();
      activate(entries[sel]);
    } else if (k === "ArrowRight") {
      const e0 = entries[sel];
      if (e0?.kind === "playbook" && e0.pb === askPb) {
        handled();
        activate(e0);
      }
    } else if (k === "Escape") {
      handled();
      close(true);
    } else if (k.length === 1 && /[a-z]/i.test(k)) {
      const i = entries.findIndex(
        (x) => x.kind === "playbook" && letterOf(x.pb) === k.toUpperCase()
      );
      if (i >= 0) {
        handled();
        setSel(i);
        activate(entries[i]);
      }
    }
  };

  const item = (e: Entry, i: number) => {
    const cls = "pb-item" + (i === sel ? " sel" : "");
    if (e.kind === "message") {
      return (
        <div
          key="message"
          id={itemId(i)}
          className={cls}
          role="menuitem"
          tabIndex={-1}
          onMouseMove={() => setSel(i)}
          onClick={() => activate(e)}
        >
          <span className="pb-name">Message…</span>
          <span className="pb-key">Ctrl+K S</span>
          <span className="pb-desc">Write to a session yourself, in the Thread tab</span>
        </div>
      );
    }
    const pb = e.pb;
    const isAsk = pb === askPb;
    const off = !pb.available;
    return (
      <div
        key={pb.id}
        ref={isAsk ? askItemRef : undefined}
        id={itemId(i)}
        className={cls + (off ? " off" : "") + (isAsk && askOpen ? " sub-open" : "")}
        role="menuitem"
        tabIndex={-1}
        aria-disabled={off || undefined}
        aria-haspopup={isAsk || undefined}
        title={off ? pb.disabled_reason || undefined : undefined}
        data-playbook={pb.id}
        onMouseMove={() => {
          setSel(i);
          if (askOpen && !isAsk) setAskOpen(false);
        }}
        onClick={() => activate(e)}
      >
        <span className="pb-name">
          {pb.label}
          {pb.id === "wrapup" && model.workers && (
            <span className="pb-cnt">{model.workers.reported}</span>
          )}
        </span>
        <span className="pb-key">{letterOf(pb)}</span>
        <span className={"pb-desc" + (off ? " pb-why" : "")}>
          {off ? pb.disabled_reason || "Not available right now" : pb.desc}
        </span>
        {isAsk && !off && (
          <span className="pb-caret" aria-hidden="true">
            ›
          </span>
        )}
      </div>
    );
  };

  const nGeneral = model.general.length;
  const nWorkers = model.workers?.items.length || 0;

  return createPortal(
    <>
      <div
        id="playbook-menu"
        className="pb-menu"
        role="menu"
        aria-label={`Work with other sessions — ${name}`}
        tabIndex={-1}
        ref={menuRef}
        aria-activedescendant={
          askOpen && askPb ? (targets.length ? sessId(askSel) : undefined) : itemId(sel)
        }
        style={{ top: 0, left: 0 }}
        onKeyDown={onKeyDown}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="pb-head">
          <b>Work with other sessions</b>
          <span className="muted">→ {name}</span>
        </div>
        {!list && !err && <div className="pb-note muted">Loading…</div>}
        {err && !list && <div className="pb-note pb-err">Couldn't load the playbooks: {err}</div>}
        {entries.slice(0, nGeneral).map((e, i) => item(e, i))}
        {model.workers && (
          <div className="pb-sec">
            {name}'s workers · {model.workers.count}
          </div>
        )}
        {entries.slice(nGeneral, nGeneral + nWorkers).map((e, i) => item(e, nGeneral + i))}
        {!model.workers && <div className="pb-sep" />}
        {item(entries[entries.length - 1], entries.length - 1)}
        <div className="pb-foot">
          Pastes the prompt into {name}'s input. Add the task, press Enter — nothing runs until
          you do. Also in the row <b>›</b> menu and the palette.
        </div>
      </div>
      {askOpen && askPb && (
        <div
          className="pb-sub"
          role="menu"
          aria-label="Ask which session"
          ref={subRef}
          style={{ top: 0, left: 0 }}
          onMouseDown={(e) => {
            // Keep the keyboard on the menu (it owns the arrow keys for both).
            e.preventDefault();
            e.stopPropagation();
          }}
        >
          <div className="pb-sub-head muted">{name} asks…</div>
          {targets.length === 0 && (
            <div className="pb-note muted">No other sessions to ask</div>
          )}
          <div className="pb-sub-list">
            {targets.map((t, i) => (
              <div
                key={t.title}
                id={sessId(i)}
                className={"pb-sess" + (i === askSel ? " hot" : "")}
                role="menuitem"
                tabIndex={-1}
                onMouseMove={() => setAskSel(i)}
                onClick={() => ask(t)}
              >
                <span className="slot">{t.slot}</span>
                <span className={"pb-dot " + (t.activity || "offline")} />
                <span className="nm">{t.name}</span>
                {t.rel && <span className="rel">{t.rel}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </>,
    document.body
  );
}
