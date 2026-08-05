/** App shell — composes the ported components and owns cross-cutting wiring:
 * capability body-classes, activity debounce feed, keymap installation, and
 * which special panes (logs / system logs / assistant chat) are open. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useConfig, useInstances } from "./state/queries";
import { tourDecision, useUi } from "./state/store";
import { noteActivity } from "./lib/stage";
import { installKeymap, type KeymapHost } from "./lib/keymap";
import { selectSession, instances as instancesSnapshot } from "./lib/sessionActions";
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
import { CommitDialog } from "./components/dialogs/CommitDialog";
import { MakePrDialog } from "./components/dialogs/MakePrDialog";
import { RenameDialog } from "./components/dialogs/RenameDialog";
import { DeviceDialog } from "./components/dialogs/DeviceDialog";
import { WorkspacesDialog } from "./components/dialogs/WorkspacesDialog";
import { RecentDialog } from "./components/dialogs/RecentDialog";
import { PromptsDialog } from "./components/dialogs/PromptsDialog";
import { SetupDialog, useDoctorAutoShow } from "./components/dialogs/SetupDialog";
import { TodoDialog } from "./components/dialogs/TodoDialog";
import { AssistantAgentDialog } from "./components/dialogs/AssistantAgentDialog";
import { WelcomeTour } from "./components/onboarding/WelcomeTour";

export default function App() {
  const { data: config } = useConfig();
  const { data: instances } = useInstances();
  const ui = useUi();
  useDoctorAutoShow();

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

  // Feed the activity debounce on every poll.
  useEffect(() => {
    for (const inst of instances || []) noteActivity(inst);
  }, [instances]);

  // Special panes (logs / system logs / assistant chat).
  const [openSpecial, setOpenSpecial] = useState<Set<string>>(new Set());
  const toggleSpecial = useCallback((kind: "logs" | "syslogs" | "chat") => {
    setOpenSpecial((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }, []);
  const specialPanes = useMemo<SpecialPaneDesc[]>(() => {
    const meta: Record<string, { title: string }> = {
      logs: { title: "MindFlock logs" },
      syslogs: { title: "System logs" },
      chat: { title: "Assistant" },
    };
    return [...openSpecial].map((kind) => ({
      key: kind,
      kind: kind as SpecialPaneDesc["kind"],
      title: meta[kind].title,
      onClose: () => toggleSpecial(kind as "logs" | "syslogs" | "chat"),
    }));
  }, [openSpecial, toggleSpecial]);

  // Keyboard: the host object gives the keymap reach into UI it can't import.
  const host = useMemo<KeymapHost>(() => {
    // Stable sidebar order: user drag order first, unknown titles appended in
    // snapshot order (mirrors ordering.ts; selection never reorders).
    const stableTitles = () => {
      const order = useUi.getState().order;
      const list = instancesSnapshot().map((i) => i.title);
      const known = order.filter((t) => list.includes(t));
      return [...known, ...list.filter((t) => !known.includes(t))];
    };
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
      cycleSession: (dir) => {
        const titles = stableTitles();
        if (!titles.length) return;
        const cur = useUi.getState().focused;
        const i = cur ? titles.indexOf(cur) : -1;
        selectSession(titles[(i + dir + titles.length) % titles.length]);
      },
      sessionAt: (index) => stableTitles()[index] ?? null,
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
      <CommitDialog />
      <MakePrDialog />
      <RenameDialog />
      <DeviceDialog />
      <WorkspacesDialog />
      <RecentDialog />
      <PromptsDialog />
      <SetupDialog />
      <TodoDialog />
      <AssistantAgentDialog />
      <CommandPalette host={host} />
      <ShortcutsSheet />
      <WelcomeTour />

      {/* Headless: event-bus toasts, favicon/title badges */}
      <EventToasts />
    </>
  );
}
