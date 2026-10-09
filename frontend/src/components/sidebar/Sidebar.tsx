/** The sidebar (ports app.js section 9's renderSidebar + the partial
 * 040-sidebar.html): doctor-warn chip, the movable bars (Usage, Tickets, Pull
 * requests, Issues, Verify, Assistant — each hideable in Customize → Sidebar),
 * the addon-bars mount, filter, bulk bar, device-grouped session list with its
 * "Recently closed (n)" link, and the footer (view picker + count + Customize
 * + Shortcuts). */

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Instance, RunTask } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances, useConfig, useDevices, useInstances } from "../../state/queries";
import { useUi, windowKey, type ViewMode } from "../../state/store";
import { toast } from "../../lib/toast";
import { canDisconnect } from "../../lib/devices";
import { viewCap } from "../grid/layout";
import { SidebarRow } from "./SidebarRow";
import { windowRows, WindowRowItem, type WindowRow } from "./WindowList";
import { SessionFilter } from "./SessionFilter";
import { SidebarResizer } from "./SidebarResizer";
import { BulkBar } from "./BulkBar";
import { useExtensionBarDefs } from "../../extensions/ExtensionBar";
import { BarSlot, barContent, SECTION_MIME } from "./SidebarBars";
import { orderedSections, SESSIONS_KEY } from "./barDefs";
import { FooterCustomize } from "./FooterCustomize";
import {
  matchesFilter,
  movedRailOrder,
  orderedInstances,
  orderedKeys,
  deviceLineage,
  placeNewRunMembers,
  placeNewWorkers,
  railNesting,
  sameNest,
  SEARCH_MIN,
  type NestInfo,
} from "./ordering";
import { childrenByParent, type ShipTask } from "../../lib/agentMessages";
import { effectiveActivity } from "../../lib/stage";
import { splitKeys, splitRail } from "../../lib/runs";
import { hideFoldedFamilies } from "../../lib/familyFold";
import { useRuns } from "../../state/runs";
import { OwnHeader, QueuedRow, RunGroupHeader } from "./RunGroupHeader";
import { computeVisible } from "../grid/layout";
import { useDoctorWarn } from "../dialogs/SetupDialog";
import { isVerifySession } from "../dialogs/verify";
import { Hint } from "../onboarding/Hint";

/** No folds (while a search filter is on). */
const NO_FOLDS: ReadonlySet<string> = new Set();

interface Props {
  onOpenChat(): void;
  onOpenTodo(): void;
}

/** The footer's view picker. One <select> rather than five buttons: it is a
 * set-once preference, and a row of five toggles read as the sidebar's loudest
 * control. */
const VIEW_MODES: ViewMode[] = ["auto", "1" as ViewMode, "2", "4", "9"];

export function Sidebar({ onOpenChat, onOpenTodo }: Props) {
  const { data: instances = [] } = useInstances();
  const { data: devices } = useDevices();
  const ui = useUi();
  const doctorWarn = useDoctorWarn();
  const { data: config } = useConfig();
  const qc = useQueryClient();
  // "Recently closed (n)" under the list: the same key and endpoint the Verify
  // dialog reads, so a reopen/forget that invalidates one refreshes both.
  const { data: closed } = useQuery({
    queryKey: ["recently-closed"],
    queryFn: () => api<unknown[]>("/api/recently-closed"),
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: false,
  });
  const closedCount = Array.isArray(closed) ? closed.length : 0;
  // Closing a session is what adds an entry — don't make the link wait a
  // minute to appear. No bus (SSR, a test) just means the poll does it.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    return ev.subscribe("session.deleted", () => {
      void qc.invalidateQueries({ queryKey: ["recently-closed"] });
    });
  }, [qc]);
  const [dragging, setDragging] = useState<string | null>(null);
  const [dropCue, setDropCue] = useState<{ title: string; cue: "above" | "below" } | null>(null);
  // Section (bar) drag — independent of the row drag above; a bar can land
  // above or below the fixed session-list anchor.
  const [secDrag, setSecDrag] = useState<string | null>(null);
  const [secCue, setSecCue] = useState<{ key: string; cue: "above" | "below" } | null>(null);
  // The addon-bars mount is created once and never re-rendered: core/slots.js
  // owns its children.
  const addonBarsRef = useRef<HTMLDivElement | null>(null);
  // Extension bars (Addon API v3): extra section keys threaded through every
  // orderedSections call so they order and drag like the built-ins.
  const extBars = useExtensionBarDefs();
  const extKeys = useMemo(() => extBars.map((b) => b.key), [extBars]);

  // Sessions the rail is FOR: the user's work. A verify run is a real session
  // (it needs a worktree and an agent that can run commands) but it is not work
  // — it is a window you open to watch for two minutes and close again, like
  // the assistant. Listed here it sat among the branches someone is actually
  // building, accumulated, and made a flock of four look like a flock of eight.
  //
  // Only the RAIL filters. `computeVisible` below still gets the full list,
  // which is what gives a verify run its pane in the grid; the Verify dialog
  // offers to open or end it, so an unlisted session is never stranded.
  const listed = useMemo(
    () => deviceLineage(instances.filter((i) => !isVerifySession(i.title))),
    [instances]
  );
  // MindFlock MCP families: each session's live workers, and the saved order
  // with every worker it has never seen slotted under its parent. An agent
  // spawns server-side, so without this a new worker files at the very bottom
  // of the rail, nowhere near the orchestrator it nests under. Rendered from
  // at once (no bottom-then-jump), persisted by the effect below; workers
  // the order already holds are never moved again — a drag owns them. Remote
  // rows too: a device section draws its rows in the same saved order, and a
  // worker another device spawned would otherwise sit at its section's end.
  const families = useMemo(() => childrenByParent(listed), [listed]);
  // …and the same for a group's members (ship lanes), which the server starts
  // over minutes as slots free up: each lands after the last of its group.
  const order = useMemo(
    () =>
      placeNewRunMembers(
        placeNewWorkers(ui.order, listed),
        listed.filter((i) => !i.device)
      ),
    [ui.order, listed]
  );
  // Sessions started together (ship lanes): their runs, for the group headers
  // and the lines still queued. Empty on a server without runs — the rail is
  // then exactly what it was.
  const { data: runsData } = useRuns();
  const runs = runsData || [];
  const runById = useMemo(() => new Map((runsData || []).map((r) => [r.id, r])), [runsData]);
  // A split / one-for-all group draws as a family (no header), so its members'
  // run view ("✓ merged back", "! conflict") comes from here instead.
  const familyTaskOf = useMemo(() => {
    const m = new Map<string, RunTask & { sameFolder?: boolean }>();
    for (const r of runsData || [])
      if (r.policy?.grouping === "together" || r.split)
        for (const t of r.tasks || [])
          if (t.title) m.set(t.title, r.mode === "same_folder" ? { ...t, sameFolder: true } : t);
    return m;
  }, [runsData]);
  useEffect(() => {
    if (order !== ui.order) ui.setOrder(order);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order]);
  const { rows: allRows } = useMemo(
    () => orderedInstances(listed, order),
    [listed, order]
  );
  const filtered = useMemo(
    () => allRows.filter((i) => matchesFilter(i, ui.filter, ui.aliases)),
    [allRows, ui.filter, ui.aliases]
  );
  // Open windows (logs / chat / verify watchers / extension panes) as rail
  // rows, interleaved with the sessions by the ONE saved order — a window's
  // order key is its grid sentinel, the key it already answers to in the MRU
  // and the grid rows. The filter narrows them by title, like any other row.
  const windows = useMemo(
    () =>
      windowRows({
        specialOpen: ui.specialOpen,
        verifyPanes: ui.verifyPanes,
        extPanes: ui.extPanes,
      }),
    [ui.specialOpen, ui.verifyPanes, ui.extPanes]
  );
  const winFiltered = useMemo(
    () => windows.filter((w) => !ui.filter || w.title.toLowerCase().includes(ui.filter)),
    [windows, ui.filter]
  );
  // The full rail in display order, UNFILTERED — what a drag reorders (the
  // filter narrows what you see, never what a drop writes back).
  const railKeys = useMemo(
    () =>
      orderedKeys(
        [...allRows.map((i) => i.title), ...windows.map((w) => w.key)],
        order
      ),
    [allRows, windows, order]
  );
  const onScreen = useMemo(
    () =>
      new Set(
        computeVisible(instances, {
          hidden: ui.hidden,
          viewMode: ui.viewMode,
          mru: ui.mru,
          order: ui.order,
        }).map((i) => i.title)
      ),
    [instances, ui.hidden, ui.viewMode, ui.mru, ui.order]
  );

  // Device grouping: only when other MindFlock devices exist on the tailnet.
  const remoteDevs = devices?.devices || [];
  const grouped = remoteDevs.length > 0;
  const localRows = filtered.filter((i) => !i.device);
  const byDev = useMemo(() => {
    const m = new Map<string, Instance[]>();
    for (const i of filtered) {
      if (!i.device) continue;
      if (!m.has(i.device)) m.set(i.device, []);
      m.get(i.device)!.push(i);
    }
    return m;
  }, [filtered]);

  // Hostnames aren't unique on a tailnet — fall back to the MagicDNS name on
  // collision (same rule as the vanilla renderer).
  const hostCounts = useMemo(() => {
    const m = new Map<string, number>();
    const selfHost = devices?.self?.host || "";
    if (selfHost) m.set(selfHost, 1);
    for (const d of remoteDevs) m.set(d.host || "", (m.get(d.host || "") || 0) + 1);
    return m;
  }, [devices, remoteDevs]);

  // One drop handler for the whole rail — session rows and window rows hand it
  // the same (dragKey, targetKey, before) shape, and movedRailOrder does the
  // merge (never wiping the slot of a row that isn't in this snapshot) plus
  // the stale-sentinel prune.
  const openWins = useMemo(() => new Set(windows.map((w) => w.key)), [windows]);
  const moveInOrder = (dragKey: string, targetKey: string, before: boolean) => {
    if (!dragKey || dragKey === targetKey) return;
    ui.setOrder(
      movedRailOrder({
        saved: order,
        live: railKeys,
        drag: dragKey,
        target: targetKey,
        before,
        // Closed verify/ext panes don't survive a reload, so their sentinels
        // must not pile up in the saved order. Session titles and the three
        // fixed windows keep their slots even while absent — a sleeping remote
        // device's rows, a closed assistant that reopens where you left it.
        stale: (k) =>
          (k.startsWith(windowKey("verify")) || k.startsWith(windowKey("ext"))) &&
          !openWins.has(k),
      })
    );
  };

  const cueFor = (title: string) =>
    dropCue && dropCue.title === title && dragging !== title ? dropCue.cue : null;

  // Reorder a dragged bar relative to a target section (a bar, or the fixed
  // "sessions" anchor). Only bars are draggable, so dragKey is never sessions.
  const moveSection = (dragKey: string, targetKey: string, before: boolean) => {
    if (!dragKey || dragKey === targetKey) return;
    const order = orderedSections(ui.barOrder, extKeys).filter((k) => k !== dragKey);
    let to = order.indexOf(targetKey);
    if (to < 0) to = order.length;
    else if (!before) to += 1;
    order.splice(to, 0, dragKey);
    ui.setBarOrder(order);
  };
  const secOver = (key: string, cue: "above" | "below") => setSecCue({ key, cue });
  const secLeave = (key: string) =>
    setSecCue((c) => (c && c.key === key ? null : c));
  // Drop-target handlers for the fixed session-list anchor (reused shape as
  // BarSlot's, but the anchor is never itself draggable).
  const sessionsDrop = {
    onDragOver: (ev: React.DragEvent) => {
      if (!ev.dataTransfer.types.includes(SECTION_MIME)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
      secOver(SESSIONS_KEY, ev.clientY - rect.top < rect.height / 2 ? "above" : "below");
    },
    onDragLeave: (ev: React.DragEvent) => {
      if (!(ev.currentTarget as HTMLElement).contains(ev.relatedTarget as Node))
        secLeave(SESSIONS_KEY);
    },
    onDrop: (ev: React.DragEvent) => {
      if (!ev.dataTransfer.types.includes(SECTION_MIME)) return;
      ev.preventDefault();
      const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
      moveSection(
        ev.dataTransfer.getData(SECTION_MIME),
        SESSIONS_KEY,
        ev.clientY - rect.top < rect.height / 2
      );
      secLeave(SESSIONS_KEY);
    },
  };

  const rowProps = {
    onDragState: setDragging,
    onDropCue: (title: string, cue: "above" | "below" | null) =>
      setDropCue(cue ? { title, cue } : null),
    onDropRow: moveInOrder,
  };

  // One rail, two row kinds. The hybrid lists are materialized from railKeys
  // so sessions and windows interleave by the saved order, and rowIdx numbers
  // straight across both. Each section's list is built ONCE, here, because the
  // render below and the published railOrder must count the same rows.
  const winOnScreen = new Set(ui.gridRows.flat());
  const toRail = (sessionRows: Instance[], wins: WindowRow[]) => {
    const byKey = new Map<string, { key: string; inst?: Instance; win?: WindowRow }>();
    for (const i of sessionRows) byKey.set(i.title, { key: i.title, inst: i });
    for (const w of wins) byKey.set(w.key, { key: w.key, win: w });
    return railKeys.filter((k) => byKey.has(k)).map((k) => byKey.get(k)!);
  };
  // Windows are local by definition, so under device grouping they ride in
  // this device's section; a remote group holds sessions only.
  // An orchestrator folded on the rail (its row's "hide N") takes its
  // sub-sessions out of the list HERE, before numbering — so they leave
  // railOrder too. Not while searching: a filter shows every match.
  const localRail = hideFoldedFamilies(
    toRail(localRows, winFiltered),
    ui.filter ? NO_FOLDS : ui.collapsedFamilies,
    ui.focused
  );
  // Ship lanes: this device's rail split into run groups (each under its
  // header, folded or not) and the rest ("On their own"). Re-sequencing only —
  // the same entries, so the published keys below are still exactly what is
  // rendered, and a folded group leaves them the way a folded device does.
  const localSplit = splitRail(localRail, runs, {
    collapsed: ui.collapsedRuns,
    act: effectiveActivity,
    filtering: !!ui.filter,
  });
  const devRails = remoteDevs.map((dev) => {
    const dkey = dev.device;
    return { dkey, rail: toRail(byDev.get(dkey) || [], []) };
  });
  // PUBLISH the rendered row order — grouping, collapse and filter applied,
  // i.e. exactly the sequence renderRail numbers below. The keymap's
  // Alt+N / Ctrl+Tab and the notification "[N]" prefixes read railOrder
  // instead of re-deriving it, so a number can never point at a row the
  // badge doesn't show.
  const displayedKeys: string[] = grouped
    ? (ui.collapsedDevices.has("__self") ? [] : splitKeys(localSplit)).concat(
        ...devRails.map(({ dkey, rail }) =>
          ui.collapsedDevices.has(dkey) ? [] : rail.map((r) => r.key)
        )
      )
    : splitKeys(localSplit);
  // Keyed by content: the array is rebuilt every render, and the store's
  // setRailOrder already no-ops on equal rows.
  const railSig = JSON.stringify(displayedKeys);
  useEffect(() => {
    useUi.getState().setRailOrder(displayedKeys);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railSig]);
  // The last NestInfo handed to each row, reused while it is unchanged so
  // the memoized rows don't all re-render on every Sidebar render.
  const nestSeen = useRef(new Map<string, NestInfo>());
  const stableNest = (key: string, n: NestInfo) => {
    const prev = nestSeen.current.get(key);
    if (prev && sameNest(prev, n)) return prev;
    nestSeen.current.set(key, n);
    return n;
  };
  let rowIdx = -1;
  // Family nesting is computed per rendered list, AFTER grouping and the
  // filter, so a worker indents only where it really sits directly under its
  // parent (or a sibling) on screen. It is paint only: `list` — and with it
  // rowIdx, the published railOrder above and every drop target — is used
  // exactly as given.
  const renderRail = (
    list: Array<{ key: string; inst?: Instance; win?: WindowRow }>,
    taskOf?: Map<string, ShipTask>
  ) => {
    const nest = railNesting(list.map((r) => ({ key: r.key, parent: r.inst?.parent })));
    return list.map((r, i) => {
      rowIdx += 1;
      const parent = r.inst?.parent;
      return r.inst ? (
        <SidebarRow
          key={r.key}
          inst={r.inst}
          idx={rowIdx}
          onScreen={onScreen.has(r.key)}
          dropCue={cueFor(r.key)}
          nest={stableNest(r.key, nest[i])}
          kids={r.inst.device ? undefined : families.get(r.key)}
          parentLive={!r.inst.device && !!parent && families.get(parent)?.includes(r.inst) === true}
          runTask={taskOf?.get(r.key) ?? null}
          leadRun={r.inst.run?.role === "lead" ? runById.get(r.inst.run.id) ?? null : null}
          {...rowProps}
        />
      ) : (
        <WindowRowItem
          key={r.key}
          row={r.win!}
          idx={rowIdx}
          onScreen={winOnScreen.has(r.key)}
          dropCue={cueFor(r.key)}
          {...rowProps}
        />
      );
    });
  };

  // This device's rail: each run group's header, its rows (unless folded) and
  // its queued lines, then "On their own" (only under a group) and the rest.
  // Rendered in exactly splitKeys' sequence, so rowIdx === the published
  // railOrder position. Headers and queued lines don't touch rowIdx.
  const groupRepo = (entries: Array<{ inst?: Instance }>) =>
    entries.find((e) => e.inst?.folder)?.inst?.folder || "";
  const renderLocal = () => (
    <>
      {localSplit.groups.map((g) => (
        <Fragment key={"run:" + g.id}>
          <RunGroupHeader group={g} repoPath={groupRepo(g.entries)} />
          {!g.collapsed && renderRail(g.entries, g.taskOf)}
          {!g.collapsed &&
            g.queued.map((t, i) => <QueuedRow key={"q:" + t.id} task={t} pos={i} groupName={g.name} />)}
        </Fragment>
      ))}
      {/* A one-for-all / split family (its lead + workers) is a group in its
          own right: never filed under "On their own". */}
      {renderRail(localSplit.families, familyTaskOf)}
      {localSplit.groups.length + localSplit.families.length > 0 && localSplit.own.length > 0 && <OwnHeader />}
      {renderRail(localSplit.own, familyTaskOf)}
    </>
  );

  const cap = viewCap(ui.viewMode);
  // Counted off `listed`, not `instances`: the footer says how many sessions
  // you have, and a verify run the rail deliberately does not show must not be
  // one of them — "6 sessions" over a list of four is the bug the filter above
  // exists to prevent, moved down a div.
  const shownCount = listed.filter((i) => !ui.hidden.has(i.title)).length;
  const countHead =
    isFinite(cap) && shownCount > cap
      ? `${cap} of ${shownCount} shown`
      : `${listed.length} session${listed.length === 1 ? "" : "s"}`;

  const searchVisible = listed.length >= SEARCH_MIN || !!ui.filter;
  // The grid's first-run card ("Get set up") shows exactly when there is no
  // session at all and the user has not onboarded — TerminalGrid's own test.
  const firstRunCard = instances.length === 0 && config?.onboarded === false;

  return (
    <aside id="sidebar">
      <SidebarResizer />
      {/* Not while the grid's first-run card is up: that card IS the setup
          checklist, and a second "setup issues" warning beside it is noise. */}
      {doctorWarn.failing && !doctorWarn.dismissed && !firstRunCard && (
        <div id="doctor-warn">
          <span className="dw-text">⚠ setup issues —</span>
          <button
            type="button"
            id="doctor-warn-open"
            className="linklike"
            onClick={() => ui.openDialogFor("settings", "doctor")}
          >
            open Doctor
          </button>
          <button
            type="button"
            id="doctor-warn-dismiss"
            title="Dismiss until reload"
            aria-label="Dismiss setup warning"
            onClick={doctorWarn.dismiss}
          >
            ✕
          </button>
        </div>
      )}
      {orderedSections(ui.barOrder, extKeys).map((key) => {
        if (key === SESSIONS_KEY) {
          return (
            <div
              key={SESSIONS_KEY}
              className={
                "sessions-block" +
                (secCue && secCue.key === SESSIONS_KEY ? ` drop-${secCue.cue}` : "")
              }
              {...sessionsDrop}
            >
              {/* Mount point: core/slots.js renders a bar here per registered addon. */}
              <div id="addon-bars" ref={addonBarsRef} />
              {searchVisible && <SessionFilter />}
              <BulkBar />
              <ul id="instance-list">
                {grouped ? (
                  <>
                    <DeviceHeader
                      label={
                        (devices?.self?.host ||
                          "This device")
                      }
                      badge={String(localRows.length)}
                      badgeOff={false}
                      collapsed={ui.collapsedDevices.has("__self")}
                      title="This device"
                      showForget={false}
                      onToggle={() => ui.toggleDeviceCollapsed("__self")}
                    />
                    {!ui.collapsedDevices.has("__self") && renderLocal()}
                    {remoteDevs.map((dev, di) => {
                      const devRows =
                        byDev.get(dev.device) || [];
                      const dkey = dev.device;
                      const collapsed = ui.collapsedDevices.has(dkey);
                      const d = dev as unknown as Record<string, unknown>;
                      let badge = "",
                        badgeOff = false;
                      if (d.connected) badge = String(devRows.length);
                      else if (!d.reachable) {
                        badge = "no mindflock";
                        badgeOff = true;
                      } else {
                        badge = "•";
                        badgeOff = true;
                      }
                      const label =
                        dev.host && hostCounts.get(dev.host) === 1 ? dev.host : dkey;
                      let note = "",
                        connectBtn = false;
                      if (!d.reachable) note = "MindFlock not reachable on that device";
                      else if (!d.remote_control)
                        note = "remote control is off on that device";
                      else if (d.needs_token) {
                        note = "needs that device's access token";
                        connectBtn = true;
                      } else if (d.error) note = String(d.error);
                      else if (d.connected && devRows.length === 0) note = "no sessions";
                      return (
                        <DeviceSection
                          key={dkey}
                          devKey={dkey}
                          label={label}
                          badge={badge}
                          badgeOff={badgeOff}
                          collapsed={collapsed}
                          title={
                            dev.host +
                            (dev.os ? "  ·  " + dev.os : "") +
                            (dev.ip ? "  ·  " + dev.ip : "") +
                            (d.member ? "  ·  One of your devices — remove it in Settings → Devices" : "")
                          }
                          // A member stays connected through the shared device
                          // key, so forgetting its token disconnects nothing.
                          showForget={canDisconnect(dev)}
                          member={!!d.member}
                          note={note}
                          connectBtn={connectBtn}
                          onToggle={() => ui.toggleDeviceCollapsed(dkey)}
                        >
                          {!collapsed && renderRail(devRails[di].rail)}
                        </DeviceSection>
                      );
                    })}
                  </>
                ) : (
                  renderLocal()
                )}
                {ui.filter && !filtered.length && !winFiltered.length && (
                  <li className="filter-empty muted">No sessions match “{ui.filter}”</li>
                )}
              </ul>
              {/* A button, never an <li>: everything inside #instance-list is
                  a rail row, and the rail's SSR tests split on '<li class="'. */}
              {closedCount > 0 && (
                <button
                  type="button"
                  id="recent-btn"
                  className="foot-link recent-link"
                  title="Reopen closed sessions or clear what they left on disk (Ctrl+Shift+T reopens the last one)"
                  onClick={() => ui.openDialogFor("recent")}
                >
                  Recently closed ({closedCount})
                </button>
              )}
            </div>
          );
        }
        if (ui.hiddenBars.has(key)) return null;
        return (
          <BarSlot
            key={key}
            barKey={key}
            dragging={secDrag === key}
            cue={secCue && secCue.key === key && secDrag !== key ? secCue.cue : null}
            onStart={setSecDrag}
            onEnd={() => {
              setSecDrag(null);
              setSecCue(null);
            }}
            onOver={secOver}
            onLeave={secLeave}
            onDropSection={moveSection}
          >
            {barContent(key, { onOpenChat, onOpenTodo })}
          </BarSlot>
        );
      })}
      <footer id="sidebar-footer">
        <div
          id="view-modes"
          title="Grid view — Auto grows with sessions; 1/2/4/9 show only the top N panes, the rest stay running but hidden until reordered into the top slots"
        >
          <span className="vm-label">View</span>
          {VIEW_MODES.map((v) => (
            <button
              key={v}
              type="button"
              className={"vm" + (ui.viewMode === v ? " active" : "")}
              data-view={v}
              onClick={() => ui.setViewMode(v)}
            >
              {v === "auto" ? "Auto" : v}
            </button>
          ))}
        </div>
        <Hint id="customize" className="hint-footer">
          More sidebar bars — like <b>Prompts</b> — are under <b>⚙ Customize</b>.
        </Hint>
        <div className="foot-row foot-tools">
          <span id="session-count">{countHead}</span>
          <FooterCustomize />
          <button
            id="shortcuts-btn"
            type="button"
            className="foot-link"
            title="Keyboard shortcuts (?)"
            onClick={() => ui.openDialogFor("shortcuts")}
          >
            ⌨ Shortcuts
          </button>
        </div>
      </footer>
    </aside>
  );
}

function DeviceHeader(props: {
  label: string;
  badge: string;
  badgeOff: boolean;
  collapsed: boolean;
  title: string;
  showForget: boolean;
  devKey?: string;
  /** One of "Your devices" (Settings → Devices). */
  member?: boolean;
  onToggle(): void;
}) {
  return (
    <li className="device-group" title={props.title} onClick={props.onToggle}>
      <span className="dev-caret">{props.collapsed ? "▸" : "▾"}</span>
      <span className="dev-name">{props.label}</span>
      {props.member && (
        <span className="dev-member" aria-label="One of your devices">
          yours
        </span>
      )}
      <span className={"dev-badge" + (props.badgeOff ? " off" : "")}>{props.badge}</span>
      {props.showForget && props.devKey && (
        <button
          className="dev-forget"
          title="Disconnect — forget this device's token"
          onClick={async (e) => {
            e.stopPropagation();
            try {
              await api(`/api/devices/${encodeURIComponent(props.devKey!)}/disconnect`, {
                method: "POST",
              });
              toast("Disconnected");
              refreshInstances();
            } catch (err) {
              toast("Disconnect failed: " + (err as Error).message);
            }
          }}
        >
          ✕
        </button>
      )}
    </li>
  );
}

function DeviceSection(props: {
  devKey: string;
  label: string;
  badge: string;
  badgeOff: boolean;
  collapsed: boolean;
  title: string;
  showForget: boolean;
  member?: boolean;
  note: string;
  connectBtn: boolean;
  onToggle(): void;
  children: React.ReactNode;
}) {
  const openDialogFor = useUi((s) => s.openDialogFor);
  return (
    <>
      <DeviceHeader {...props} />
      {!props.collapsed && props.note && (
        <li className="device-note muted">
          <span className="dev-note-text">{props.note}</span>
          {props.connectBtn && (
            <button
              className="dev-connect"
              onClick={(e) => {
                e.stopPropagation();
                openDialogFor("device", props.devKey);
              }}
            >
              Connect…
            </button>
          )}
        </li>
      )}
      {props.children}
    </>
  );
}
