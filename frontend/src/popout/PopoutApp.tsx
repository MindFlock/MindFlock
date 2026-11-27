/** The app of a pop-out window (lib/popout.ts): ONE session in an OS window of
 * its own, torn off the main window like a browser tab.
 *
 * What it is: a slim window bar (the drag region, the session's dot and
 * name, a handle to drag it back onto the main window, and "Back to main
 * window") over the session's own pane — `<Pane embedded>`, the chrome-less
 * pane the Sky cards draw: every tab, no grip, no hide.
 *
 * What it is not: the main app. No rail, no grid, no top bar, no extension
 * host, no onboarding, no app-wide toasts or OS notifications (the main
 * window raises those), and it never writes the shared saved layout
 * (state/store.ts `save`). The keymap keeps only the shortcuts that act on
 * this pane; Ctrl+W here is "Back to main window", never Close. */

import { useEffect, useRef, useState, type DragEvent } from "react";
import type { Instance } from "../api/types";
import { queryClient, refreshInstances, useInstances, type EventEnvelope } from "../state/queries";
import { useUi, type DialogName } from "../state/store";
import {
  SESSION_DRAG_MIME,
  announcePopout,
  closeThisPopout,
  popoutTitle,
  POPOUT_PARAM,
} from "../lib/popout";
import { backToMainWindow, installKeymap, type KeymapHost } from "../lib/keymap";
import { hasNativeWindowControls, inShell, isFullScreen, onFullScreenChanged } from "../lib/shell";
import { forceActivity, noteActivity } from "../lib/stage";
import { agentBlocked, agentBlockedState, deriveSessionState } from "../lib/sessionState";
import { useSetup } from "../components/setup/useSetup";
import { sessionLabel } from "../lib/sessionLabel";
import { realRename } from "../lib/aliasSync";
import { focusTerm, peekTerm } from "../lib/terminals";
import { toast } from "../lib/toast";
import { Pane } from "../components/grid/Pane";
import { CommitDialog } from "../components/dialogs/CommitDialog";
import { MakePrDialog } from "../components/dialogs/MakePrDialog";
import { MoveSessionDialog } from "../components/dialogs/MoveSessionDialog";
import { MemoryConfirm } from "../components/MemoryConfirm";
import { FolderPickerWindow } from "../components/FolderPickerWindow";
import { RouteStartMenu } from "../components/flights/RouteStartMenu";
import "./PopoutApp.css";

/** How long the "was closed" notice stays before the window closes itself. */
export const GONE_CLOSE_MS = 5000;

/** How long load waits for the pane's terminal to exist before giving up on
 * handing it the keyboard. */
const FOCUS_TRY_MS = 4000;

/** The dialogs this window mounts: the ones a pane's own buttons and menus
 * open on this session. Anything else (Settings, New, Intake, Rename — whose
 * per-title moves rewrite the main window's saved layout — …) belongs to the
 * main window, which this window would only open behind. */
export const POPOUT_DIALOGS: ReadonlySet<DialogName> = new Set<DialogName>(["commit", "make-pr", "move"]);

/** The keymap's host: every entry that needs it is main-window only, and a
 * pop-out never fires those (lib/keymap.ts `popout`). */
const NO_HOST: KeymapHost = {
  togglePalette: () => {},
  toggleShortcuts: () => {},
  focusFilter: () => {},
  cycleWindow: () => {},
  rowAt: () => null,
  openDoctor: () => {},
};

/** `search` with the pop-out param swapped for `to` (a rename): the URL a
 * reload of this window should come back to. */
export function searchWithTitle(search: string, to: string): string {
  const p = new URLSearchParams(search);
  p.set(POPOUT_PARAM, to);
  return "?" + p.toString();
}

/** The name a person reads for a session: its alias, else its ticket/PR
 * label, else its title (the rail's rule). */
function shownName(title: string, inst: Instance | undefined, alias: string | undefined): string {
  if (alias) return alias;
  if (!inst) return title;
  const own = (inst as unknown as { display_title?: string }).display_title || inst.title;
  return sessionLabel(own, inst.branch || "").text || title;
}

/** Start dragging the session back: the main window's drop target reads
 * {@link SESSION_DRAG_MIME} and docks it. */
function onHandleDragStart(e: DragEvent, title: string) {
  e.dataTransfer.setData(SESSION_DRAG_MIME, title);
  e.dataTransfer.setData("text/plain", title);
  e.dataTransfer.effectAllowed = "move";
}

function StatusDot({ inst }: { inst: Instance }) {
  const { data: setup } = useSetup();
  const blocked = agentBlocked(inst, setup?.logins);
  const ss = blocked ? agentBlockedState(blocked) : deriveSessionState(inst);
  return <span className={"popout-dot tone-" + ss.tone + " st-" + ss.state} title={ss.label} aria-label={ss.label} />;
}

/** The window bar: the OS drag region, with room for the macOS traffic
 * lights (left) or the desktop shell's own – □ ✕ (right, `#mf-winctl`). */
export function PopoutBar({
  title,
  name,
  inst,
  macLights = false,
  shellButtons = false,
  onBack,
}: {
  title: string;
  name: string;
  inst?: Instance;
  macLights?: boolean;
  shellButtons?: boolean;
  onBack: () => void;
}) {
  return (
    <div
      id="popout-bar"
      data-mac-lights={macLights ? "" : undefined}
      data-shell-buttons={shellButtons ? "" : undefined}
    >
      <span
        className="popout-grip"
        draggable
        title="Drag onto the main window to put it back"
        aria-label="Drag back to the main window"
        onDragStart={(e) => onHandleDragStart(e, title)}
      >
        ⠿
      </span>
      {inst && <StatusDot inst={inst} />}
      <span className="popout-name" title={title}>
        {name}
      </span>
      <span className="popout-drag" />
      <button type="button" className="popout-back" title="Back to main window (Ctrl+W)" onClick={onBack}>
        Back to main window
      </button>
    </div>
  );
}

/** The session is not there (closed, deleted, or never was): say so calmly,
 * and close the window shortly. */
export function GoneNotice({ name, onClose }: { name: string; onClose: () => void }) {
  return (
    <div className="popout-gone" role="status">
      <p>“{name}” was closed.</p>
      <p className="popout-gone-sub">This window closes in a few seconds.</p>
      <button type="button" className="popout-close" onClick={onClose} autoFocus>
        Close
      </button>
    </div>
  );
}

export function PopoutApp() {
  const [title, setTitle] = useState(() => popoutTitle() || "");
  const titleRef = useRef(title);
  titleRef.current = title;
  const { data: instances } = useInstances();
  const inst = instances?.find((i) => i.title === title);
  const alias = useUi((s) => s.aliases[title]);
  const name = shownName(title, inst, alias);
  // Gone only once the list has loaded: before that, "not in it" means
  // nothing (and a failed fetch keeps it unloaded — a server restarting is
  // not a session closing).
  const gone = !!instances && !inst;

  // Evaluated once, as the top bar does: the shell can't grow or lose its
  // title bar mid-run.
  const [mac] = useState(hasNativeWindowControls);
  const [shellButtons] = useState(() => inShell() && !hasNativeWindowControls());
  const [fullScreen, setFullScreen] = useState(false);
  useEffect(() => {
    if (!mac) return;
    let live = true;
    void isFullScreen().then((f) => {
      if (live) setFullScreen(f);
    });
    const off = onFullScreenChanged(setFullScreen);
    return () => {
      live = false;
      off();
    };
  }, [mac]);

  // A rename (the event bus; the main window's message on the browser path):
  // show the new name, and make a reload come back to it.
  const rename = useRef((to: string) => {
    if (!to || to === titleRef.current) return;
    titleRef.current = to;
    setTitle(to);
    try {
      history.replaceState(history.state, "", location.pathname + searchWithTitle(location.search, to) + location.hash);
    } catch {
      /* no history (tests) */
    }
  }).current;

  // Tell the main window this session is here (browser path), and follow its
  // requests: close (it docked the session) and renames.
  useEffect(
    () => announcePopout(() => titleRef.current, { onClose: () => closeThisPopout(), onRename: rename }),
    [rename]
  );

  // The events this one pane needs: the main window's EventToasts does this
  // (and the toasts) for everyone there.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    const isReplay = (env: EventEnvelope) => typeof ev.isReplay === "function" && ev.isReplay(env);
    const offs = [
      ev.subscribe("session.renamed", (env) => {
        const r = realRename(env);
        if (!r || r.from !== titleRef.current) return;
        if (isReplay(env)) {
          // History after a reconnect: only while the snapshot agrees it
          // happened (the old title may since belong to a new session).
          const rows = queryClient.getQueryData<Instance[]>(["instances"]);
          if (!rows || !rows.some((x) => x.title === r.to) || rows.some((x) => x.title === r.from)) return;
        }
        rename(r.to);
        refreshInstances();
      }),
      ev.subscribe("session.activity_changed", (env) => {
        if (env.session !== titleRef.current) return;
        forceActivity(env.session, env.new || "idle");
        refreshInstances();
      }),
      ev.subscribe("session.deleted", (env) => {
        if (env.session === titleRef.current) refreshInstances();
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [rename]);

  // The status dot's debounce feed (App.tsx does this for every row).
  useEffect(() => {
    if (inst) noteActivity(inst);
  }, [inst]);

  useEffect(() => {
    const want = (name || title) + " — MindFlock";
    const put = () => {
      if (document.title !== want) document.title = want;
    };
    put();
    // lib/titleBadge (the main window's "● (n) MindFlock") loads with the
    // bundle and rewrites the title when its favicon logo arrives: this
    // window keeps its own.
    const el = document.querySelector("title");
    if (!el || typeof MutationObserver === "undefined") return;
    const mo = new MutationObserver(put);
    mo.observe(el, { childList: true, characterData: true, subtree: true });
    return () => mo.disconnect();
  }, [name, title]);

  // The pane draws focused, and the keymap's focused-session shortcuts
  // (Ctrl+K chords, Alt+G) act on it.
  useEffect(() => {
    useUi.getState().setFocused(title || null);
  }, [title]);

  useEffect(() => installKeymap(NO_HOST), []);

  // Typing goes to the agent: hand its terminal the keyboard once it exists.
  const hasInst = !!inst;
  useEffect(() => {
    if (!hasInst) return;
    const t0 = Date.now();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tryFocus = () => {
      if (peekTerm(titleRef.current, "agent")) {
        const el = document.activeElement;
        if (!el || el === document.body) focusTerm(titleRef.current);
        return;
      }
      if (Date.now() - t0 < FOCUS_TRY_MS) timer = setTimeout(tryFocus, 100);
    };
    tryFocus();
    return () => clearTimeout(timer);
  }, [hasInst]);

  // Gone: close the window after the notice has been read.
  useEffect(() => {
    if (!gone) return;
    const t = setTimeout(() => closeThisPopout(), GONE_CLOSE_MS);
    return () => clearTimeout(t);
  }, [gone]);

  // A dialog only the main window has: don't open it behind nothing.
  useEffect(
    () =>
      useUi.subscribe((s, prev) => {
        const d = s.openDialog;
        if (!d || d === prev.openDialog || POPOUT_DIALOGS.has(d)) return;
        useUi.getState().closeDialog();
        toast("That’s in the main window");
      }),
    []
  );

  // Our own handle dropped back onto this window is not a paste of the title
  // into the terminal.
  useEffect(() => {
    const ours = (e: globalThis.DragEvent) => !!e.dataTransfer?.types?.includes(SESSION_DRAG_MIME);
    const over = (e: globalThis.DragEvent) => {
      if (!ours(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "none";
    };
    const drop = (e: globalThis.DragEvent) => {
      if (!ours(e)) return;
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("dragover", over, true);
    window.addEventListener("drop", drop, true);
    return () => {
      window.removeEventListener("dragover", over, true);
      window.removeEventListener("drop", drop, true);
    };
  }, []);

  return (
    <PopoutView
      title={title}
      name={name}
      inst={inst}
      gone={gone}
      macLights={mac && !fullScreen}
      shellButtons={shellButtons}
    />
  );
}

/** The window as drawn (SSR-testable: no effects). */
export function PopoutView({
  title,
  name,
  inst,
  gone,
  macLights = false,
  shellButtons = false,
}: {
  title: string;
  name: string;
  inst?: Instance;
  gone: boolean;
  macLights?: boolean;
  shellButtons?: boolean;
}) {
  return (
    <div id="popout-root">
      <PopoutBar
        title={title}
        name={name}
        inst={inst}
        macLights={macLights}
        shellButtons={shellButtons}
        onBack={backToMainWindow}
      />
      <div className="popout-body">
        {inst ? (
          <Pane inst={inst} embedded />
        ) : gone ? (
          <GoneNotice name={name} onClose={closeThisPopout} />
        ) : (
          <div className="popout-loading">Loading…</div>
        )}
      </div>
      <CommitDialog />
      <MakePrDialog />
      <MoveSessionDialog />
      <MemoryConfirm />
      <FolderPickerWindow />
      <RouteStartMenu />
    </div>
  );
}
