/** App shell — composes the ported components and owns cross-cutting wiring:
 * capability body-classes, activity debounce feed, keymap installation, the
 * special-pane descriptor list (logs / system logs / assistant chat / verify /
 * extension — open state lives in the UI store; the sidebar's Windows rows
 * close them), and feeding the extension host its enabled set. */

import { useCallback, useEffect, useMemo } from "react";
import { api } from "./api/client";
import { setWheelDamping } from "./lib/terminals";
import { installAliasSync } from "./lib/aliasSync";

/** How often a guest's page re-says which session it is on (the server
 * forgets a focus after 90 s: core.guests.FOCUS_TTL_S). */
export const GUEST_FOCUS_HEARTBEAT_MS = 30_000;
import { useConfig, useExtensions, useInstances, useIntakeWarm } from "./state/queries";
import { useUi, type SpecialKind } from "./state/store";
import { followAutopilot, noteActivity, reconcileLoopReset } from "./lib/stage";
import { installKeymap, type KeymapHost } from "./lib/keymap";
import { selectRailKey } from "./lib/sessionActions";
import { syncExtensions } from "./extensions/host";
import { ExtensionDialog } from "./extensions/ExtensionDialog";
import { TopBar } from "./components/TopBar";
import { ConnBanner } from "./components/ConnBanner";
import { MemoryNotice } from "./components/MemoryNotice";
import { StateNotice } from "./components/StateNotice";
import { EventToasts } from "./components/EventToasts";
import { Sidebar } from "./components/sidebar/Sidebar";
import { TerminalGrid, type SpecialPaneDesc } from "./components/grid/TerminalGrid";
import { CommandPalette } from "./components/palette/CommandPalette";
import { ShortcutsSheet } from "./components/palette/ShortcutsSheet";
import { NewSessionDialog } from "./components/dialogs/NewSessionDialog";
import { SettingsDialog } from "./components/settings/SettingsDialog";
import { IntakeDialog } from "./components/intake/IntakeDialog";
import { CustomizeDialog } from "./components/customize/CustomizeDialog";
import { PromptsDialog } from "./components/dialogs/PromptsDialog";
import { VerifyDialog } from "./components/dialogs/VerifyDialog";
import { CommitDialog } from "./components/dialogs/CommitDialog";
import { PeerSessionViewer } from "./components/PeerSessionViewer";
import { MakePrDialog } from "./components/dialogs/MakePrDialog";
import { AskDialog } from "./components/dialogs/AskDialog";
import { RenameDialog } from "./components/dialogs/RenameDialog";
import { GuestNameDialog } from "./components/dialogs/GuestNameDialog";
import { RecentDialog } from "./components/dialogs/RecentDialog";
import { SetupDialog, useDoctorAutoShow } from "./components/dialogs/SetupDialog";
import { ConnectGitHubDialog } from "./components/dialogs/ConnectGitHub";
import { TodoDialog } from "./components/dialogs/TodoDialog";
import { AssistantAgentDialog } from "./components/dialogs/AssistantAgentDialog";
import { RedZonesDialog } from "./components/dialogs/RedZonesDialog";
import { WelcomeTour } from "./components/onboarding/WelcomeTour";
import { CoachTourHost } from "./components/onboarding/CoachTour";
import { Breaks } from "./components/breaks/Breaks";
import { openSettings } from "./lib/openSettings";
import { FolderPickerWindow } from "./components/FolderPickerWindow";
import { installSurfaceHash } from "./lib/surfaceHash";

/** At or under this window width the main screen goes single-column. */
const NARROW_PX = 760;

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

  // First-run: ONE surface. The checklist card on the empty grid (TerminalGrid)
  // while a required step is to do, the "Setup 3/5" chip (below) after, and
  // the Setup modal when either opens it. App never opens a tour: the welcome
  // card's one automatic opening is WelcomeTour's, each dialog's tour is
  // useAutoTour's, and replays are the palette's, the ? sheet's, a dialog's
  // "?" and Settings → General → Tours.
  // Seed the server's alias mirror once per load: renames made before the
  // mirror existed live only in this browser's localStorage, and ntfy pushes
  // are formatted server-side. Merge-only on the server, so this cannot erase
  // renames another browser synced. Fire-and-forget — an older server without
  // the endpoint just keeps naming pushes by raw title.
  useEffect(() => {
    // The terminal's wheel speed below one line is enforced browser-side:
    // apply the stored speed at boot, not only when Settings → General mounts
    // (it used to reset to a full line on every reload until then).
    api<{ speed?: number }>("/api/scroll-speed")
      .then((s) => {
        if (s?.speed) setWheelDamping(s.speed);
      })
      .catch(() => {});
  }, []);

  // Renames: seed-and-adopt for the owner, read-only for a guest, and live
  // on `session.renamed` for both (lib/aliasSync). Waits for /api/config: the
  // two paths differ, and a guest's seed POST would be a 403.
  const isGuest = config ? !!config.guest : null;
  useEffect(() => {
    if (isGuest === null) return;
    installAliasSync(isGuest);
  }, [isGuest]);

  // A guest's page tells the owner where it is: the session it has selected,
  // on every change and every 30 s while open (core.guests.note_focus) — the
  // owner's rail shows a pill with the guest's name on that row and can
  // follow it. Never from the owner's own browser, and not before the guest
  // has said who they are (GuestNameDialog): the pill carries their name.
  const focusedTitle = ui.focused || "";
  const guestNamed = !!config?.guest?.name;
  useEffect(() => {
    if (!isGuest || !guestNamed || !focusedTitle) return;
    const report = () =>
      api("/api/instances/" + encodeURIComponent(focusedTitle) + "/focus", { json: {} }).catch(() => {});
    report();
    const t = setInterval(report, GUEST_FOCUS_HEARTBEAT_MS);
    return () => clearInterval(t);
  }, [isGuest, guestNamed, focusedTitle]);

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

  // A phone-narrow window: the sidebar becomes an overlay that starts closed
  // (▯ in the top bar opens it), "Auto" view shows one pane, and picking a
  // session closes the overlay again. Nothing here is saved — widen the
  // window and the sidebar is wherever you left it.
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(max-width: " + NARROW_PX + "px)");
    const apply = () => {
      useUi.getState().setNarrow(mq.matches);
      document.body.classList.toggle("narrow", mq.matches);
    };
    apply();
    mq.addEventListener?.("change", apply);
    return () => mq.removeEventListener?.("change", apply);
  }, []);
  useEffect(() => {
    const s = useUi.getState();
    if (s.narrow && !s.sidebarHidden && ui.focused) s.toggleSidebar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ui.focused]);

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
      focusFilter: () => {
        const box = document.getElementById("session-filter") as HTMLInputElement | null;
        // Hidden by Customize: summon it — it mounts focused (SessionFilter).
        if (box) box.focus();
        else useUi.getState().setFilterPeek(true);
      },
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
      openDoctor: () => openSettings("doctor"),
    };
  }, []);

  useEffect(() => installKeymap(host), [host]);
  // New / Intake / Verify / Settings / Templates in the URL hash: browser
  // Back closes the open one, a link opens it (lib/surfaceHash).
  useEffect(() => installSurfaceHash(), []);

  return (
    <>
      <ConnBanner />
      <MemoryNotice />
      <StateNotice />
      <TopBar />
      <Sidebar
        onOpenChat={() => toggleSpecial("chat")}
        onOpenTodo={() => useUi.getState().openDialogFor("todo")}
      />
      <TerminalGrid specialPanes={specialPanes} />

      {/* Dialogs (each renders null unless open) */}
      <NewSessionDialog />
      <SettingsDialog onOpenSysLogsPane={() => toggleSpecial("syslogs")} />
      <IntakeDialog />
      <CustomizeDialog />
      <PromptsDialog />
      <VerifyDialog />
      <CommitDialog />
      <PeerSessionViewer />
      <MakePrDialog />
      <RenameDialog />
      <GuestNameDialog />
      <RecentDialog />
      <SetupDialog />
      <ConnectGitHubDialog />
      <TodoDialog />
      <AssistantAgentDialog />
      <RedZonesDialog />
      <ExtensionDialog />
      {/* A session asking you something: opens by itself (lib/ask.ts). */}
      <AskDialog />
      <CommandPalette host={host} />
      <ShortcutsSheet />
      <WelcomeTour />
      {/* The coach-mark tour on screen, if any (lib/tours): last, so its card
          and ring sit above whichever dialog it walks. */}
      <CoachTourHost />
      {/* The one floating folder picker every path field's Browse… opens
          (portalled to body, above the dialogs, under the toast). */}
      <FolderPickerWindow />

      {/* Break reminder + the idle flock (Settings → General; both off-screen
          until their timers fire) */}
      <Breaks />

      {/* Headless: event-bus toasts, favicon/title badges */}
      <EventToasts />
    </>
  );
}
