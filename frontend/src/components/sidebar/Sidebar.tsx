/** The sidebar (ports app.js section 9's renderSidebar + the partial
 * 040-sidebar.html): doctor-warn chip, overall usage, automation + PR-review +
 * assistant bars (each hideable via the footer Customize menu), the addon-bars
 * mount, filter, bulk bar, device-grouped session list, and the footer
 * (view modes + count + customize + shortcuts). */

import { useEffect, useMemo, useRef, useState } from "react";
import type { Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances, useDevices, useInstances } from "../../state/queries";
import { useUi, windowKey, type ViewMode } from "../../state/store";
import { toast } from "../../lib/toast";
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
  SEARCH_MIN,
} from "./ordering";
import { computeVisible } from "../grid/layout";
import { useDoctorWarn } from "../dialogs/SetupDialog";
import { isVerifySession } from "../dialogs/verify";
import { Hint } from "../onboarding/Hint";

interface Props {
  onOpenChat(): void;
  onOpenTodo(): void;
}

const VIEW_MODES: ViewMode[] = ["auto", "1" as ViewMode, "2", "4", "9"];

export function Sidebar({ onOpenChat, onOpenTodo }: Props) {
  const { data: instances = [] } = useInstances();
  const { data: devices } = useDevices();
  const ui = useUi();
  const doctorWarn = useDoctorWarn();
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
  const listed = useMemo(() => instances.filter((i) => !isVerifySession(i.title)), [instances]);
  const { rows: allRows } = useMemo(
    () => orderedInstances(listed, ui.order),
    [listed, ui.order]
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
        ui.order
      ),
    [allRows, windows, ui.order]
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
    const selfHost = (devices?.self as unknown as { host?: string } | null)?.host || "";
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
        saved: ui.order,
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
  const localRail = toRail(localRows, winFiltered);
  const devRails = remoteDevs.map((dev) => {
    const dkey = (dev as { device?: string }).device || dev.name;
    return { dkey, rail: toRail(byDev.get(dkey) || [], []) };
  });
  // PUBLISH the rendered row order — grouping, collapse and filter applied,
  // i.e. exactly the sequence renderRail numbers below. The keymap's
  // Alt+N / Ctrl+Tab and the notification "[N]" prefixes read railOrder
  // instead of re-deriving it, so a number can never point at a row the
  // badge doesn't show.
  const displayedKeys: string[] = grouped
    ? (ui.collapsedDevices.has("__self") ? [] : localRail.map((r) => r.key)).concat(
        ...devRails.map(({ dkey, rail }) =>
          ui.collapsedDevices.has(dkey) ? [] : rail.map((r) => r.key)
        )
      )
    : localRail.map((r) => r.key);
  // Keyed by content: the array is rebuilt every render, and the store's
  // setRailOrder already no-ops on equal rows.
  const railSig = JSON.stringify(displayedKeys);
  useEffect(() => {
    useUi.getState().setRailOrder(displayedKeys);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railSig]);
  let rowIdx = -1;
  const renderRail = (list: Array<{ key: string; inst?: Instance; win?: WindowRow }>) =>
    list.map((r) => {
      rowIdx += 1;
      return r.inst ? (
        <SidebarRow
          key={r.key}
          inst={r.inst}
          idx={rowIdx}
          onScreen={onScreen.has(r.key)}
          dropCue={cueFor(r.key)}
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

  return (
    <aside id="sidebar">
      <SidebarResizer />
      {doctorWarn.failing && !doctorWarn.dismissed && (
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
      <Hint
        id="welcome"
        action={{ label: "Open Settings", onClick: () => ui.openDialogFor("settings") }}
      >
        <b>Welcome to MindFlock.</b> Connect your coding CLI and tools in Settings,
        then start a session to get going.
      </Hint>
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
                        ((devices?.self as unknown as { host?: string } | null)?.host ||
                          "This device")
                      }
                      badge={String(localRows.length)}
                      badgeOff={false}
                      collapsed={ui.collapsedDevices.has("__self")}
                      title="This device"
                      showForget={false}
                      onToggle={() => ui.toggleDeviceCollapsed("__self")}
                    />
                    {!ui.collapsedDevices.has("__self") && renderRail(localRail)}
                    {remoteDevs.map((dev, di) => {
                      const devRows =
                        byDev.get((dev as { device?: string }).device || dev.name) || [];
                      const dkey = (dev as { device?: string }).device || dev.name;
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
                            (dev.ip ? "  ·  " + dev.ip : "")
                          }
                          showForget={!!d.has_token}
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
                  renderRail(localRail)
                )}
                {ui.filter && !filtered.length && !winFiltered.length && (
                  <li className="filter-empty muted">No sessions match “{ui.filter}”</li>
                )}
              </ul>
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
            {barContent(key, {
              onOpenChat,
              onOpenTodo,
              openDialogFor: ui.openDialogFor,
            })}
          </BarSlot>
        );
      })}
      <footer id="sidebar-footer">
        <div
          id="view-modes"
          title="Grid view — Auto grows with sessions; 2/4/9 show only the top N panes, the rest stay running but hidden until reordered into the top slots"
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
          Showing just the essentials. Add <b>PR review</b>, <b>issue handling</b> and
          more anytime from <b>⚙ Customize</b> below.
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
  onToggle(): void;
}) {
  return (
    <li className="device-group" title={props.title} onClick={props.onToggle}>
      <span className="dev-caret">{props.collapsed ? "▸" : "▾"}</span>
      <span className="dev-name">{props.label}</span>
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
