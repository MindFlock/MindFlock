/** The sidebar (ports app.js section 9's renderSidebar + the partial
 * 040-sidebar.html): doctor-warn chip, overall usage, automation + PR-review +
 * assistant bars (each hideable via the footer Customize menu), the addon-bars
 * mount, filter, bulk bar, device-grouped session list, and the footer
 * (view modes + count + customize + shortcuts). */

import { useMemo, useRef, useState } from "react";
import type { Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances, useDevices, useInstances } from "../../state/queries";
import { useUi, type ViewMode } from "../../state/store";
import { toast } from "../../lib/toast";
import { viewCap } from "../grid/layout";
import { SidebarRow } from "./SidebarRow";
import { SessionFilter } from "./SessionFilter";
import { SidebarResizer } from "./SidebarResizer";
import { BulkBar } from "./BulkBar";
import { BarSlot, barContent, SECTION_MIME } from "./SidebarBars";
import { orderedSections, SESSIONS_KEY } from "./barDefs";
import { FooterCustomize } from "./FooterCustomize";
import { matchesFilter, orderedInstances, SEARCH_MIN } from "./ordering";
import { computeVisible } from "../grid/layout";
import { useDoctorWarn } from "../dialogs/SetupDialog";
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

  const { rows: allRows } = useMemo(
    () => orderedInstances(instances, ui.order),
    [instances, ui.order]
  );
  const filtered = useMemo(
    () => allRows.filter((i) => matchesFilter(i, ui.filter, ui.aliases)),
    [allRows, ui.filter, ui.aliases]
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

  const moveInOrder = (dragTitle: string, targetTitle: string, before: boolean) => {
    if (!dragTitle || dragTitle === targetTitle) return;
    const order = allRows.map((i) => i.title).filter((t) => t !== dragTitle);
    let to = order.indexOf(targetTitle);
    if (to < 0) to = order.length;
    else if (!before) to += 1;
    order.splice(to, 0, dragTitle);
    ui.setOrder(order);
  };

  const cueFor = (title: string) =>
    dropCue && dropCue.title === title && dragging !== title ? dropCue.cue : null;

  // Reorder a dragged bar relative to a target section (a bar, or the fixed
  // "sessions" anchor). Only bars are draggable, so dragKey is never sessions.
  const moveSection = (dragKey: string, targetKey: string, before: boolean) => {
    if (!dragKey || dragKey === targetKey) return;
    const order = orderedSections(ui.barOrder).filter((k) => k !== dragKey);
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

  let rowIdx = -1;
  const renderRows = (list: Instance[]) =>
    list.map((inst) => {
      rowIdx += 1;
      return (
        <SidebarRow
          key={inst.title}
          inst={inst}
          idx={rowIdx}
          onScreen={onScreen.has(inst.title)}
          dropCue={cueFor(inst.title)}
          {...rowProps}
        />
      );
    });

  const cap = viewCap(ui.viewMode);
  const shownCount = instances.filter((i) => !ui.hidden.has(i.title)).length;
  const countHead =
    isFinite(cap) && shownCount > cap
      ? `${cap} of ${shownCount} shown`
      : `${instances.length} session${instances.length === 1 ? "" : "s"}`;

  const searchVisible = instances.length >= SEARCH_MIN || !!ui.filter;

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
      {orderedSections(ui.barOrder).map((key) => {
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
                    {!ui.collapsedDevices.has("__self") && renderRows(localRows)}
                    {remoteDevs.map((dev) => {
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
                          {!collapsed && renderRows(devRows)}
                        </DeviceSection>
                      );
                    })}
                  </>
                ) : (
                  renderRows(localRows)
                )}
                {ui.filter && !filtered.length && (
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
