/** App shell — composes the ported components and owns cross-cutting wiring:
 * capability body-classes, activity debounce feed, keymap installation, the
 * special-pane descriptor list (logs / system logs / assistant chat / verify /
 * extension — open state lives in the UI store; the sidebar's Windows rows
 * close them), and feeding the extension host its enabled set. */

import { useCallback, useEffect, useMemo, useRef } from "react";
import { api } from "./api/client";
import { useConfig, useExtensions, useInstances, useIntakeWarm } from "./state/queries";
import { tourDecision, useUi, type SpecialKind } from "./state/store";
import { followAutopilot, noteActivity, reconcileLoopReset } from "./lib/stage";
import { installKeymap, type KeymapHost } from "./lib/keymap";
import { selectRailKey } from "./lib/sessionActions";
import { syncExtensions } from "./extensions/host";
import { ExtensionDialog } from "./extensions/ExtensionDialog";
import { TopBar } from "./components/TopBar";
import { ConnBanner } from "./components/ConnBanner";
import { StateNotice } from "./components/StateNotice";
import { VoiceInput } from "./components/VoiceInput";
import { EventToasts } from "./components/EventToasts";
import { Sidebar } from "./components/sidebar/Sidebar";
import { TerminalGrid, type SpecialPaneDesc } from "./components/grid/TerminalGrid";
import { CommandPalette } from "./components/palette/CommandPalette";
import { ShortcutsSheet } from "./components/palette/ShortcutsSheet";
import { NewSessionDialog } from "./components/dialogs/NewSessionDialog";
import { SettingsDialog } from "./components/settings/SettingsDialog";
import { IntakeDialog } from "./components/intake/IntakeDialog";
import { VerifyDialog } from "./components/dialogs/VerifyDialog";
import { CommitDialog } from "./components/dialogs/CommitDialog";
import { MakePrDialog } from "./components/dialogs/MakePrDialog";
import { RenameDialog } from "./components/dialogs/RenameDialog";
import { DeviceDialog } from "./components/dialogs/DeviceDialog";
import { RecentDialog } from "./components/dialogs/RecentDialog";
import { PromptsDialog } from "./components/dialogs/PromptsDialog";
import { SetupDialog, useDoctorAutoShow } from "./components/dialogs/SetupDialog";
import { TodoDialog } from "./components/dialogs/TodoDialog";
import { AssistantAgentDialog } from "./components/dialogs/AssistantAgentDialog";
import { WelcomeTour } from "./components/onboarding/WelcomeTour";
import { Breaks } from "./components/breaks/Breaks";

export default function App() {
  const { data: config } = useConfig();
  const { data: instances } = useInstances();
  const ui = useUi();
  useDoctorAutoShow();
  // Intake's lists are upstream fan-outs, so they have to be fetched before the
  // dialog exists or the first thing it shows is a spinner. Gated on a
  // connected ticketing source — the same condition that decides whether the
  // dialog has anything to show at all.
  useIntakeWarm(!!config?.caps?.ticketing);

  // First-run onboarding: pop the welcome walkthrough once, for a user who is
  // new by the SERVER's reckoning and still has hints enabled — tourDecision()
  // holds the rule. Until /api/config resolves it answers "wait" and this decides
  // nothing, which is why the flags stay in the deps while the latch is a
  // separate ref: the config query refetches on its own and re-arming hints from
  // Settings re-runs this effect, and neither should be able to restart the
  // twelve slides mid-session. finishTour() flips tourDone so it never reopens on
  // its own — a replay is an explicit action from Settings.
  //
  // Watching the tour is deliberately NOT reported back. general.onboarded means
  // "this user has a session", which is why the server sets it on a create and
  // nowhere else; a POST from here wrote it for anyone who merely clicked past the
  // slideshow, and it took both first-run surfaces down with it — the grid's setup
  // card and the auto-opening dependency checklist — for a user who still had no
  // tmux and so could not create a session at all.
  const tourDecided = useRef(false);
  useEffect(() => {
    if (tourDecided.current) return;
    const decision = tourDecision({
      tourDone: ui.tourDone,
      hintsEnabled: ui.hintsEnabled,
      onboarded: config?.onboarded,
    });
    if (decision === "wait") return;
    tourDecided.current = true;
    if (decision === "open") ui.openTour();
  }, [config?.onboarded, ui.tourDone, ui.hintsEnabled, ui.openTour]);

  // Seed the server's alias mirror once per load: renames made before the
  // mirror existed live only in this browser's localStorage, and ntfy pushes
  // are formatted server-side. Merge-only on the server, so this cannot erase
  // renames another browser synced. Fire-and-forget — an older server without
  // the endpoint just keeps naming pushes by raw title.
  useEffect(() => {
    const aliases = useUi.getState().aliases;
    if (Object.keys(aliases).length) {
      api("/api/aliases", { json: { aliases } }).catch(() => {});
    }
  }, []);

  // Capability gating: body classes drive the caps-gate CSS (and addon CSS).
  useEffect(() => {
    const caps = config?.caps;
    document.body.classList.toggle("no-git", caps ? !caps.git : false);
    document.body.classList.toggle("no-tailscale", caps ? !caps.tailscale : false);
    document.body.classList.toggle("no-ticketing", caps ? !caps.ticketing : false);
  }, [config?.caps]);

  // Sidebar visibility class (CSS drops the sidebar column on <body>); let
  // the grid resize, then refit terminals.
  useEffect(() => {
    document.body.classList.toggle("sidebar-collapsed", ui.sidebarHidden);
    const t = setTimeout(() => window.dispatchEvent(new Event("resize")), 60);
    return () => clearTimeout(t);
  }, [ui.sidebarHidden]);

  // Sidebar width (dragged from its right edge) drives the body grid column.
  // SidebarResizer writes the var directly while dragging; this is what makes
  // the committed width survive a reload.
  useEffect(() => {
    document.body.style.setProperty("--sidebar-w", ui.sidebarWidth + "px");
  }, [ui.sidebarWidth]);

  // Feed the activity debounce on every poll, and retire the post-Make-PR loop
  // pin once the real stage has left "pr". Without the reconcile the pin is
  // permanent for the life of the page: after one PR that session's guided
  // button stays on "Commit…" and never offers Push again.
  useEffect(() => {
    for (const inst of instances || []) {
      noteActivity(inst);
      reconcileLoopReset(inst);
      // Guaranteed path for following an autopilot run: the event gives
      // sub-second response, this makes sure a dropped or missed event cannot
      // leave the run unfollowed.
      followAutopilot(inst);
    }
  }, [instances]);

  // Special panes (logs / system logs / assistant chat / verify runs /
  // extension panes). All in the store: the sidebar's Windows rows are what
  // close them, so their open state can't live in this component.
  const specialOpen = useUi((s) => s.specialOpen);
  const verifyPanes = useUi((s) => s.verifyPanes);
  const extPanes = useUi((s) => s.extPanes);

  // FEED THE EXTENSION HOST ITS ENABLED SET. syncExtensions() is what
  // deactivates an extension that was disabled or removed — draining its
  // registrations and closing its panes and dialog — so this effect is also
  // the reap for extension windows: there is no separate "close panes whose
  // extension left" pass, deactivateExtension does it. Driven from the query,
  // not from the Settings toggle, so a disable made in another tab lands here
  // on the next manifest refetch.
  //
  // Guarded on a SUCCESSFUL query, deliberately (the verify reap's non-empty
  // guard, same reason): `data` is undefined while the first fetch is in
  // flight, and on a failed refetch the query reports an error while keeping
  // the last good data — syncing either would tear every extension down over
  // a network hiccup, dirty grid cells and typed SQL included.
  const extQuery = useExtensions();
  useEffect(() => {
    if (!extQuery.isSuccess || !extQuery.data) return;
    syncExtensions(extQuery.data);
  }, [extQuery.isSuccess, extQuery.data]);

  // REAP A WATCH WINDOW WHOSE SESSION HAS GONE. A verify run is a real session
  // and it can end without anybody closing its pane: it finishes, it is
  // cancelled, its plan is deleted, or it dies and `test_plans.prune` releases
  // the plan back to `due`. The pane is driven off a title in the store, so
  // none of those reach it — the head goes on saying "Verifying" over an empty
  // body forever, because the terminal socket behind it has nothing to attach
  // to. It reads as the feature being broken, and the fix is to stop rendering
  // a window onto something that is not there.
  //
  // Guarded on a NON-EMPTY list, deliberately: `instances` is `undefined` while
  // the first poll is in flight and `[]` on a failed one, and reaping against
  // either would shut a pane the user is watching every time the network
  // hiccups.
  useEffect(() => {
    if (!instances || !instances.length || !verifyPanes.length) return;
    const live = new Set(instances.map((i) => i.title));
    for (const session of verifyPanes) {
      if (!live.has(session)) useUi.getState().closeVerifyPane(session);
    }
  }, [instances, verifyPanes]);
  const toggleSpecial = useCallback(
    (kind: SpecialKind) => useUi.getState().toggleSpecial(kind),
    []
  );
  // The descs carry no close callback: the pane head's ✕ (SpecialPane's
  // CloseBtn) and the sidebar's window rows both derive the close action from
  // kind+ref, so the two controls can never disagree.
  const specialPanes = useMemo<SpecialPaneDesc[]>(() => {
    const meta: Record<string, { title: string }> = {
      logs: { title: "MindFlock logs" },
      syslogs: { title: "System logs" },
      chat: { title: "Assistant" },
    };
    const fixed: SpecialPaneDesc[] = specialOpen.map((kind) => ({
      key: kind,
      kind: kind as SpecialPaneDesc["kind"],
      title: meta[kind].title,
    }));
    // Verify runs are watch windows: read-only, closable, absent from the
    // session rail as SESSIONS (they are not work) — but their WINDOWS get a
    // sidebar row like every other open window.
    const runs: SpecialPaneDesc[] = verifyPanes.map((session) => ({
      key: "verify:" + session,
      kind: "verify",
      title: session,
      session,
    }));
    // Extension panes: one per open pane key, titled live from the store.
    const ext: SpecialPaneDesc[] = extPanes.map((p) => ({
      key: "ext:" + p.key,
      kind: "ext",
      title: p.title,
      extKey: p.key,
    }));
    return fixed.concat(runs, ext);
  }, [specialOpen, verifyPanes, extPanes]);

  // Keyboard: the host object gives the keymap reach into UI it can't import.
  const host = useMemo<KeymapHost>(() => {
    // The rail EXACTLY as the sidebar rendered and numbered it — sessions and
    // windows, drag order, device grouping, collapse and filter applied. Read
    // from railOrder (the sidebar publishes it after each render) rather than
    // re-derived, so the Alt+N number badges and the shortcuts cannot
    // disagree about what the Nth row is. Every earlier re-derivation drifted
    // somewhere: verify sessions, device grouping, the live filter.
    const stableKeys = () => useUi.getState().railOrder;
    const toggleDialog = (name: "palette" | "shortcuts") => {
      const s = useUi.getState();
      if (s.openDialog === name) s.closeDialog();
      else s.openDialogFor(name);
    };
    return {
      togglePalette: () => toggleDialog("palette"),
      toggleShortcuts: () => toggleDialog("shortcuts"),
      focusFilter: () =>
        (document.getElementById("session-filter") as HTMLInputElement | null)?.focus(),
      cycleWindow: (dir) => {
        const keys = stableKeys();
        if (!keys.length) return;
        const s = useUi.getState();
        // The cursor is the last-SELECTED row: the MRU head while it's still
        // on the rail (selecting a window never moves `focused` — that's the
        // keyboard target, a session-only concept), else the focused session.
        const cur = keys.includes(s.mru[0]) ? s.mru[0] : s.focused;
        const i = cur ? keys.indexOf(cur) : -1;
        selectRailKey(keys[(i + dir + keys.length) % keys.length]);
      },
      rowAt: (index) => stableKeys()[index] ?? null,
      openDoctor: () => useUi.getState().openDialogFor("settings", "doctor"),
    };
  }, []);

  useEffect(() => installKeymap(host), [host]);

  return (
    <>
      <ConnBanner />
      <StateNotice />
      <TopBar />
      <Sidebar
        onOpenChat={() => toggleSpecial("chat")}
        onOpenTodo={() => useUi.getState().openDialogFor("todo")}
      />
      <TerminalGrid specialPanes={specialPanes} />
      <VoiceInput />

      {/* Dialogs (each renders null unless open) */}
      <NewSessionDialog />
      <SettingsDialog onOpenSysLogsPane={() => toggleSpecial("syslogs")} />
      <IntakeDialog />
      <VerifyDialog />
      <CommitDialog />
      <MakePrDialog />
      <RenameDialog />
      <DeviceDialog />
      <RecentDialog />
      <PromptsDialog />
      <SetupDialog />
      <TodoDialog />
      <AssistantAgentDialog />
      <ExtensionDialog />
      <CommandPalette host={host} />
      <ShortcutsSheet />
      <WelcomeTour />

      {/* Break reminder + the idle flock (Settings → General; both off-screen
          until their timers fire) */}
      <Breaks />

      {/* Headless: event-bus toasts, favicon/title badges */}
      <EventToasts />
    </>
  );
}
